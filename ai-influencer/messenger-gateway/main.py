import asyncio
import logging
import base64
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Awaitable, Callable, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from openai import AsyncOpenAI

from adapters.discord_adapter import DiscordAdapter
from config import settings
from prompts import (
    NOTEBOOKLM_REPORT_PROMPT,
    SCRIPT_REWRITE_SYSTEM_PROMPT,
    SCRIPT_ENDING_LINE,
    SUBTITLE_SCRIPT_OPENING_LINE,
    TTS_SCRIPT_REWRITE_PROMPT_BASE,
    TTS_SCRIPT_OPENING_LINE,
    build_subtitle_retry_prompt,
    build_tts_retry_prompt,
    build_tts_script_rewrite_instruction,
    build_tts_script_prompt,
    build_subtitle_from_tts_prompt,
)
from utils.file_naming import build_filename
from services.storage_service import presign_s3_uri, put_bytes_and_presign
from models.job import (
    AutoReportRequest,
    ChannelSelectRequest,
    CharacterAvatarRequest,
    ConfirmActionRequest,
    HeygenSmokeTestRequest,
    IncomingMessageRequest,
    ListJobsRequest,
    ManualGenerateRequest,
    MessengerSource,
    ReportMessageRequest,
    ReportSelectRequest,
    ReportToTtsRequest,
    ReportToVideoRequest,
    SendAudioRequest,
    SendConfirmRequest,
    SendReportRequest,
    SendTextRequest,
    SendVideoPreviewRequest,
    TtsActionRequest,
    VideoActionRequest,
)
from services import job_service, n8n_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 싱글턴 어댑터 및 httpx 클라이언트
_http_client: Optional[httpx.AsyncClient] = None
_discord_adapter: Optional[DiscordAdapter] = None
_DISCORD_ATTACHMENT_LIMIT_BYTES = 10 * 1024 * 1024
_HEYGEN_REQUEST_TIMEOUT_SECONDS = 30.0


def _extract_valid_basename(job_id: str, filename: object) -> Optional[str]:
    if not isinstance(filename, str) or not filename:
        return None
    pattern = rf"^(\d{{8}}-{re.escape(job_id)})\.(txt|wav|mp4)$"
    match = re.match(pattern, filename)
    if not match:
        return None
    return match.group(1)


def _resolve_media_basename(
    job_id: str,
    existing_script_json: object = None,
    candidate_filename: Optional[str] = None,
) -> str:
    parsed = _as_script_json(existing_script_json)
    media_names = parsed.get("media_names")
    if isinstance(media_names, dict):
        for key in ("report_filename", "audio_filename", "video_filename"):
            candidate = media_names.get(key)
            basename = _extract_valid_basename(job_id, candidate)
            if basename:
                return basename
    candidate_basename = _extract_valid_basename(job_id, candidate_filename)
    if candidate_basename:
        return candidate_basename
    return build_filename(job_id, "txt").rsplit(".", 1)[0]


def _normalize_filename(
    job_id: str,
    ext: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    normalized = f"{_resolve_media_basename(job_id, existing_script_json, candidate)}.{ext}"
    if candidate and candidate != normalized:
        logger.info("[file-naming] normalize %s filename job_id=%s from=%s to=%s", ext, job_id, candidate, normalized)
    return normalized


def _normalize_report_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "txt", candidate, existing_script_json)


def _normalize_audio_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "wav", candidate, existing_script_json)


def _normalize_video_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "mp4", candidate, existing_script_json)


def _normalize_log_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "json", candidate, existing_script_json)


def _heygen_api_headers() -> dict[str, str]:
    api_key = settings.heygen_api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="HEYGEN_API_KEY is not configured")
    return {"X-Api-Key": api_key}


async def _heygen_get_json(path: str, *, base_url: Optional[str] = None) -> dict:
    if _http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client is not initialized")
    target_base = (base_url or settings.heygen_api_base_url).rstrip("/")
    url = f"{target_base}/{path.lstrip('/')}"
    try:
        resp = await _http_client.get(
            url,
            headers=_heygen_api_headers(),
            timeout=_HEYGEN_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        detail = e.response.text.strip() or str(e)
        raise HTTPException(status_code=502, detail=f"HeyGen API request failed: {detail}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HeyGen API request failed: {e}") from e

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="HeyGen API returned a non-JSON response")
    return payload


def _as_script_json(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return dict(parsed)
        except json.JSONDecodeError:
            return {}
    return {}


def _merge_script_json_with_media_names(
    existing: object,
    *,
    report_filename: Optional[str] = None,
    log_filename: Optional[str] = None,
    tts_script_filename: Optional[str] = None,
    audio_filename: Optional[str] = None,
    video_filename: Optional[str] = None,
    script_text: Optional[str] = None,
    subtitle_script_text: Optional[str] = None,
    tts_script_text: Optional[str] = None,
    notebooklm_report_text: Optional[str] = None,
    log_s3_uri: Optional[str] = None,
    tts_script_s3_uri: Optional[str] = None,
    heygen_avatar_id: Optional[str] = None,
    script_rewrite_prompt: Optional[str] = None,
    script_rewrite_status: Optional[str] = None,
    script_rewrite_error: Optional[str] = None,
    generated_content_id: Optional[str] = None,
    generated_content_error: Optional[str] = None,
    heygen_use_avatar_iv_model: Optional[bool] = None,
    tts_error_type: Optional[str] = None,
    tts_error_detail: Optional[str] = None,
) -> dict:
    merged = _as_script_json(existing)
    resolved_subtitle_script_text = subtitle_script_text
    if resolved_subtitle_script_text is None and script_text is not None:
        resolved_subtitle_script_text = script_text
    resolved_tts_script_text = tts_script_text
    if resolved_tts_script_text is None and script_text is not None:
        resolved_tts_script_text = script_text
    if resolved_subtitle_script_text is not None:
        merged["subtitle_script_text"] = resolved_subtitle_script_text
        merged["script_text"] = resolved_subtitle_script_text
        merged["script"] = resolved_subtitle_script_text  # backward compatibility
        merged["script_summary"] = resolved_subtitle_script_text[:200]
    if resolved_tts_script_text is not None:
        merged["tts_script_text"] = resolved_tts_script_text
    if notebooklm_report_text is not None:
        if notebooklm_report_text:
            merged["notebooklm_report_text"] = notebooklm_report_text
        else:
            merged.pop("notebooklm_report_text", None)
    if log_s3_uri is not None:
        if log_s3_uri:
            merged["log_s3_uri"] = log_s3_uri
        else:
            merged.pop("log_s3_uri", None)
    if tts_script_s3_uri is not None:
        if tts_script_s3_uri:
            merged["tts_script_s3_uri"] = tts_script_s3_uri
        else:
            merged.pop("tts_script_s3_uri", None)
    if heygen_avatar_id is not None:
        normalized_avatar_id = (heygen_avatar_id or "").strip()
        if normalized_avatar_id:
            merged["heygen_avatar_id"] = normalized_avatar_id
        else:
            merged.pop("heygen_avatar_id", None)
    if script_rewrite_prompt is not None:
        if script_rewrite_prompt:
            merged["script_rewrite_prompt"] = script_rewrite_prompt
        else:
            merged.pop("script_rewrite_prompt", None)
    if script_rewrite_status is not None:
        if script_rewrite_status:
            merged["script_rewrite_status"] = script_rewrite_status
        else:
            merged.pop("script_rewrite_status", None)
    if script_rewrite_error is not None:
        if script_rewrite_error:
            merged["script_rewrite_error"] = script_rewrite_error
        else:
            merged.pop("script_rewrite_error", None)
    if generated_content_id is not None:
        normalized_content_id = str(generated_content_id).strip()
        if normalized_content_id:
            merged["generated_content_id"] = normalized_content_id
        else:
            merged.pop("generated_content_id", None)
    if generated_content_error is not None:
        normalized_content_error = str(generated_content_error).strip()
        if normalized_content_error:
            merged["generated_content_error"] = normalized_content_error
        else:
            merged.pop("generated_content_error", None)
    if heygen_use_avatar_iv_model is not None:
        merged["heygen_use_avatar_iv_model"] = bool(heygen_use_avatar_iv_model)
    if tts_error_type is not None:
        normalized_tts_error_type = str(tts_error_type).strip()
        if normalized_tts_error_type:
            merged["tts_error_type"] = normalized_tts_error_type
        else:
            merged.pop("tts_error_type", None)
    if tts_error_detail is not None:
        normalized_tts_error_detail = str(tts_error_detail).strip()
        if normalized_tts_error_detail:
            merged["tts_error_detail"] = normalized_tts_error_detail
        else:
            merged.pop("tts_error_detail", None)
    media_names = merged.get("media_names")
    if not isinstance(media_names, dict):
        media_names = {}
    if report_filename:
        media_names["report_filename"] = report_filename
    if log_filename:
        media_names["log_filename"] = log_filename
    if tts_script_filename:
        media_names["tts_script_filename"] = tts_script_filename
    if audio_filename:
        media_names["audio_filename"] = audio_filename
    if video_filename:
        media_names["video_filename"] = video_filename
    if media_names:
        merged["media_names"] = media_names
    return merged


class ReportPreparationError(RuntimeError):
    def __init__(self, message: str, *, script_json: Optional[dict] = None):
        super().__init__(message)
        self.script_json = script_json or {}


def _discord_attachment_limit_bytes() -> int:
    limit = settings.media_max_discord_file_bytes
    if limit <= 0:
        return _DISCORD_ATTACHMENT_LIMIT_BYTES
    return limit


def _normalize_manual_job_id(job_id: str) -> str:
    return (job_id or "").strip()


def _format_job_summary(job: dict) -> str:
    job_id = job.get("id", "")
    status = job.get("status", "")
    return f"{job_id[:8]}  status={status}"


def _to_iso8601(value: object) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[attr-defined]
    return str(value)


def _is_transient_notebooklm_error(err: Exception) -> bool:
    msg = str(err).lower()
    transient_markers = (
        "temporary failure in name resolution",
        "name or service not known",
        "nodename nor servname provided",
        "connection refused",
        "connection reset by peer",
        "connect timeout",
        "read timeout",
        "timed out",
        "network is unreachable",
        "no route to host",
    )
    return any(marker in msg for marker in transient_markers)


def _default_tts_caption(*, auto_trigger_wf12: bool) -> str:
    if auto_trigger_wf12:
        return "🔊 TTS 완료. 영상 제작 모드로 WF-12(HeyGen)를 자동 실행합니다."
    return "🔊 TTS 완료본입니다. 일반 승인 또는 고화질 승인을 선택한 뒤 최종 확인을 진행하세요."


def _get_subtitle_script_text(script_json: dict) -> str:
    return (
        script_json.get("subtitle_script_text")
        or script_json.get("script_text")
        or script_json.get("script")
        or ""
    ).strip()


def _get_tts_script_text(script_json: dict) -> str:
    return (
        script_json.get("tts_script_text")
        or script_json.get("subtitle_script_text")
        or script_json.get("script_text")
        or script_json.get("script")
        or ""
    ).strip()


def _get_job_avatar_override(script_json: dict) -> str:
    return (
        script_json.get("heygen_avatar_id")
        or script_json.get("avatar_id")
        or ""
    ).strip()


async def _persist_job_avatar_override(job_id: str, existing_script_json: object, avatar_id: str) -> dict:
    merged_script = _merge_script_json_with_media_names(
        existing_script_json,
        heygen_avatar_id=avatar_id,
    )
    return await job_service.update_job(job_id, script_json=merged_script)


async def _register_generated_content(
    *,
    job_id: str,
    script_text: str,
    content_url: str,
) -> tuple[str, str]:
    service_base_url = settings.heygen_pipeline_service_url.rstrip("/")
    if not service_base_url:
        return "", ""

    cleaned_script_text = (script_text or "").strip()
    cleaned_content_url = (content_url or "").strip()
    if not cleaned_script_text or not cleaned_content_url:
        return "", ""

    try:
        resp = await _http_client.post(
            f"{service_base_url}/register-content",
            json={
                "job_id": job_id,
                "script_text": cleaned_script_text,
                "content_url": cleaned_content_url,
            },
            timeout=settings.heygen_pipeline_service_timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        content_id = str(payload.get("content_id") or "").strip()
        if content_id:
            logger.info("[generated-content] registered job_id=%s content_id=%s", job_id, content_id)
        return content_id, ""
    except Exception as e:
        logger.warning("[generated-content] register failed job_id=%s: %s", job_id, e)
        return "", str(e)


async def _resolve_heygen_avatar_id(job: dict, *, requested_avatar_id: str = "") -> tuple[str, str]:
    explicit_avatar_id = (requested_avatar_id or "").strip()
    if explicit_avatar_id:
        return explicit_avatar_id, "request"

    script_json = _as_script_json(job.get("script_json"))
    job_avatar_id = _get_job_avatar_override(script_json)
    if job_avatar_id:
        return job_avatar_id, "job"

    character_id = str(job.get("character_id") or "").strip()
    if character_id:
        character = await job_service.get_character(character_id)
        if character is not None:
            character_avatar_id = str(character.get("heygen_avatar_id") or "").strip()
            if character_avatar_id:
                return character_avatar_id, "character"

    env_avatar_id = settings.heygen_avatar_id.strip()
    if env_avatar_id:
        return env_avatar_id, "env"

    raise HTTPException(
        status_code=400,
        detail="No HeyGen avatar configured. Provide avatar_id, set job override, or configure character default.",
    )


def _extract_completion_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    parts.append(stripped)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _extract_json_object(text: str) -> dict:
    # LLM 응답은 코드블록/설명문이 섞일 수 있어서
    # "가능하면 JSON 객체 하나"만 안전하게 뽑아낸다.
    candidate = (text or "").strip()
    if not candidate:
        return {}
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
        candidate = candidate.strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_topic_keywords(raw_report_text: str) -> list[str]:
    lines = [line.strip() for line in (raw_report_text or "").strip().splitlines() if line.strip()]
    if not lines:
        return []

    blocked_lines = {
        "신고",
        "메모 추가",
        "공유",
        "share",
        "content_copy",
        "thumb_up",
        "thumb_down",
        "collapse_content",
        "more_horiz",
        "스튜디오",
    }
    blocked_line_patterns = (
        r"^소스 \d+개 기반$",
        r"^기반:소스.*$",
        r"^소스 \d+개$",
        r"^NotebookLM이 부정확한 정보를 표시할 수 있으므로.*$",
    )

    title_line = ""
    for line in lines[:8]:
        if line in blocked_lines:
            continue
        if any(re.fullmatch(pattern, line) for pattern in blocked_line_patterns):
            continue
        if re.fullmatch(r"[a-z_]+", line):
            continue
        title_line = line
        break

    if not title_line:
        return []

    normalized = re.sub(r"[\[\]\(\)\{\}:,./*\"'“”‘’\-–—]+", " ", title_line)
    candidates = re.findall(r"[A-Za-z0-9.+#-]{2,}|[가-힣]{2,}", normalized)
    blocked = {
        "기술",
        "보고서",
        "분석",
        "메커니즘",
        "학문적",
        "통찰",
        "시대",
        "서론",
        "개념적",
        "정의",
        "전략적",
        "시사점",
        "글로벌",
        "현대",
        "차세대",
        "소스",
        "기반",
        "개",
        "NotebookLM",
        "부정확한",
        "표시할",
        "있으므로",
        "대답을",
        "다시",
        "확인하세요",
    }
    keywords: list[str] = []
    for candidate in candidates:
        token = candidate.strip()
        if len(token) < 2 or token in blocked:
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords[:5]


def _extract_supporting_facts(raw_report_text: str) -> list[str]:
    body = (raw_report_text or "").strip()
    if not body:
        return []
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) > 1:
        body = "\n".join(lines[1:])
    sentences = re.split(r"(?<=[.!?다요])\s+|\n+", body)
    facts: list[str] = []
    for sentence in sentences:
        fact = sentence.strip(" -\t")
        if len(fact) < 18:
            continue
        if fact not in facts:
            facts.append(fact)
        if len(facts) >= 5:
            break
    return facts


_SCRIPT_MIN_CHARS = 320
_SCRIPT_MAX_CHARS = 400
_SCRIPT_REWRITE_MAX_ATTEMPTS = 5
_REPORT_SCRIPT_RETRY_MAX_ATTEMPTS = 15


def _script_char_count(script_text: str) -> int:
    # 줄바꿈은 포맷 요소라 길이 계산에서 제외하고, 실제 문장 길이만 본다.
    return len((script_text or "").replace("\r", "").replace("\n", ""))


def _sanitize_prompt_text(text: str) -> str:
    sanitized = (text or "").replace("\x00", "")
    return sanitized.encode("utf-8", "ignore").decode("utf-8")


def _build_tts_request_body(script_text: str) -> dict:
    cleaned_script_text = (script_text or "").strip()
    if not cleaned_script_text:
        raise RuntimeError("script_text is required")

    tts_body = {
        "text": cleaned_script_text,
        "text_lang": settings.tts_text_lang or "ko",
        "prompt_lang": settings.tts_prompt_lang or settings.tts_text_lang or "ko",
        "media_type": "wav",
        "streaming_mode": False,
        "top_k": settings.tts_top_k,
        "sample_steps": settings.tts_sample_steps,
        "super_sampling": settings.tts_super_sampling,
        "fragment_interval": settings.tts_fragment_interval,
    }

    ref_audio_path = settings.tts_ref_audio_path.strip()
    prompt_text = settings.tts_prompt_text.strip()
    if ref_audio_path or prompt_text:
        if not (ref_audio_path and prompt_text):
            raise RuntimeError("TTS_REF_AUDIO_PATH와 TTS_PROMPT_TEXT는 함께 설정해야 합니다.")
        tts_body["ref_audio_path"] = ref_audio_path
        tts_body["prompt_text"] = prompt_text

    return tts_body


def _classify_tts_error(detail: str, *, status_code: Optional[int] = None) -> str:
    normalized = (detail or "").lower()
    runtime_markers = (
        "averaged_perceptron_tagger_eng",
        "resource '",
        "nltk",
        "traceback",
        '"exception":"',
        "searched in:",
    )
    request_markers = (
        "script_text is required",
        "ref_audio_path is required",
        "prompt_text cannot be empty",
        "tts_ref_audio_path와 tts_prompt_text는 함께 설정해야 합니다.",
        "invalid",
        "unprocessable",
    )

    if any(marker in normalized for marker in runtime_markers):
        return "tts_server_runtime_error"
    if any(marker in normalized for marker in request_markers):
        return "request_validation_error"
    if status_code is not None and status_code >= 500:
        return "tts_server_runtime_error"
    if status_code in {400, 401, 403, 404, 405, 409, 422}:
        return "request_validation_error"
    return "tts_server_runtime_error"


def _extract_tts_error_type(detail: str) -> str:
    match = re.search(r"\[(request_validation_error|tts_server_runtime_error)\]", detail or "")
    if match:
        return match.group(1)
    return _classify_tts_error(detail)


def _format_tts_api_error(status_code: int, detail: str) -> str:
    error_type = _classify_tts_error(detail, status_code=status_code)
    normalized_detail = (detail or "").strip() or "empty response"
    return f"TTS API failed [{error_type}]: {status_code} {normalized_detail}"


def _non_empty_line_count(script_text: str) -> int:
    return len([line for line in (script_text or "").splitlines() if line.strip()])


def _build_job_prompt_log(
    *,
    job_id: str,
    existing_job: Optional[dict],
    notebooklm_prompt: str,
    raw_report_text: str,
    rewrite_prompt: str,
    rewrite_instruction: str,
) -> dict:
    return {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "messenger_user_id": str((existing_job or {}).get("messenger_user_id") or ""),
        "messenger_channel_id": str((existing_job or {}).get("messenger_channel_id") or ""),
        "character_id": str((existing_job or {}).get("character_id") or ""),
        "notebooklm": {
            "prompt_final": notebooklm_prompt,
            "report_raw": raw_report_text,
        },
        "rewrite": {
            "instruction_base": TTS_SCRIPT_REWRITE_PROMPT_BASE,
            "instruction_custom": rewrite_prompt,
            "instruction_final": rewrite_instruction,
            "report_attempts": [],
            "tts_attempts": [],
            "subtitle_attempts": [],
            "final": {
                "status": "pending",
                "error": "",
                "tts_script_text": "",
                "subtitle_script_text": "",
                "tts_prompt_final": "",
                "subtitle_prompt_final": "",
            },
        },
    }


def _latest_rewrite_response_text(prompt_log: dict, attempt_key: str) -> str:
    rewrite = prompt_log.get("rewrite") if isinstance(prompt_log, dict) else None
    attempts = rewrite.get(attempt_key) if isinstance(rewrite, dict) else None
    if not isinstance(attempts, list) or not attempts:
        return ""
    latest = attempts[-1]
    if not isinstance(latest, dict):
        return ""
    return _sanitize_prompt_text(str(latest.get("response_text") or "").strip())


def _build_report_retry_notice(*, attempt: int, max_attempts: int, reason: str) -> str:
    clipped_reason = _sanitize_prompt_text((reason or "").strip())[:160] or "원인 미상"
    return (
        f"⚠️ 대본 생성에 실패해 다시 시도합니다. ({attempt}/{max_attempts})\n"
        f"사유: {clipped_reason}"
    )


def _upload_prompt_log_file(
    *,
    job_id: str,
    log_payload: dict,
    existing_script_json: object,
) -> tuple[object | None, dict]:
    normalized_filename = _normalize_log_filename(job_id, None, existing_script_json)
    try:
        content = json.dumps(log_payload, ensure_ascii=False, indent=2).encode("utf-8")
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_logs,
            filename=normalized_filename,
            content=content,
            content_type="application/json; charset=utf-8",
        )
        merged_script = _merge_script_json_with_media_names(
            existing_script_json,
            log_filename=normalized_filename,
            log_s3_uri=stored.s3_uri,
        )
        logger.info("[storage] prompt log uploaded job_id=%s filename=%s s3_uri=%s", job_id, normalized_filename, stored.s3_uri)
        return stored, merged_script
    except Exception as e:
        logger.error("[storage] prompt log upload failed job_id=%s: %s", job_id, e)
        merged_script = _merge_script_json_with_media_names(
            existing_script_json,
            log_filename=normalized_filename,
            log_s3_uri="",
        )
        return None, merged_script


def _validate_subtitle_script(tts_script_text: str, subtitle_script_text: str) -> None:
    subtitle_len = _script_char_count(subtitle_script_text)
    if not (_SCRIPT_MIN_CHARS <= subtitle_len <= _SCRIPT_MAX_CHARS):
        raise RuntimeError(
            f"subtitle_script_text length out of range: {subtitle_len} (expected {_SCRIPT_MIN_CHARS}-{_SCRIPT_MAX_CHARS})"
        )
    subtitle_lines = [line.strip() for line in subtitle_script_text.splitlines() if line.strip()]
    tts_lines = [line.strip() for line in tts_script_text.splitlines() if line.strip()]
    if len(subtitle_lines) != len(tts_lines):
        raise RuntimeError(
            f"subtitle_script_text line count mismatch: tts={len(tts_lines)} subtitle={len(subtitle_lines)}"
        )
    if not subtitle_lines:
        raise RuntimeError("subtitle_script_text is empty")
    if not subtitle_lines[0].startswith(SUBTITLE_SCRIPT_OPENING_LINE):
        raise RuntimeError("subtitle_script_text opening line mismatch")
    if not subtitle_lines[-1].endswith(SCRIPT_ENDING_LINE):
        raise RuntimeError("subtitle_script_text ending line mismatch")
    if tts_lines:
        if not tts_lines[0].startswith(TTS_SCRIPT_OPENING_LINE):
            raise RuntimeError("tts_script_text opening line mismatch before subtitle conversion")
        if subtitle_lines[0] != tts_lines[0]:
            raise RuntimeError("subtitle_script_text first line does not preserve tts line")
        if not tts_lines[-1].endswith(SCRIPT_ENDING_LINE):
            raise RuntimeError("tts_script_text ending line mismatch before subtitle conversion")


def _validate_tts_script(raw_report_text: str, tts_script_text: str) -> None:
    keywords = _extract_topic_keywords(raw_report_text)
    if keywords:
        tts_ok = any(keyword in tts_script_text for keyword in keywords)
        if not tts_ok:
            raise RuntimeError(f"script rewrite topic drift detected; missing keywords={keywords}")
    tts_len = _script_char_count(tts_script_text)
    if not (_SCRIPT_MIN_CHARS <= tts_len <= _SCRIPT_MAX_CHARS):
        raise RuntimeError(
            f"tts_script_text length out of range: {tts_len} (expected {_SCRIPT_MIN_CHARS}-{_SCRIPT_MAX_CHARS})"
        )
    tts_lines = [line.strip() for line in tts_script_text.splitlines() if line.strip()]
    if not tts_lines:
        raise RuntimeError("tts_script_text is empty")
    if not tts_lines[0].startswith(TTS_SCRIPT_OPENING_LINE):
        raise RuntimeError("tts_script_text opening line mismatch")
    if not tts_lines[-1].endswith(SCRIPT_ENDING_LINE):
        raise RuntimeError("tts_script_text ending line mismatch")


async def _rewrite_report_to_script(
    raw_report_text: str,
    rewrite_instruction: str,
    *,
    prompt_log: dict,
    max_attempts: int = _SCRIPT_REWRITE_MAX_ATTEMPTS,
    seed_tts_script_text: str = "",
    seed_subtitle_script_text: str = "",
) -> tuple[str, str]:
    # NotebookLM 원문 보고서를 한 번 더 정제해
    # 자막용/ TTS용 스크립트를 동시에 만든다.
    api_key = settings.openai_api_key.strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured for script rewrite")

    client = AsyncOpenAI(api_key=api_key)
    try:
        last_error: Exception | None = None
        fact_lines = "\n".join(f"- {fact}" for fact in _extract_supporting_facts(raw_report_text)) or "- 원문 보고서의 핵심 사실을 그대로 사용한다."
        tts_script_text = _sanitize_prompt_text((seed_tts_script_text or "").strip())
        for attempt in range(max(1, max_attempts)):
            use_retry_prompt = attempt > 0 or bool(tts_script_text)
            tts_prompt = (
                build_tts_retry_prompt(
                    raw_report_text=raw_report_text,
                    rewrite_instruction=rewrite_instruction,
                    previous_script_text=tts_script_text,
                    char_count=_script_char_count(tts_script_text),
                    fact_lines=fact_lines,
                )
                if use_retry_prompt
                else build_tts_script_prompt(
                    raw_report_text=raw_report_text,
                    fact_lines=fact_lines,
                    rewrite_instruction=rewrite_instruction,
                )
            )
            tts_prompt = _sanitize_prompt_text(tts_prompt)
            attempt_record = {
                "attempt": len(prompt_log["rewrite"]["tts_attempts"]) + 1,
                "prompt": tts_prompt,
                "response_text": "",
                "char_count": 0,
                "validation_error": "",
            }
            response = await client.chat.completions.create(
                model=settings.script_rewrite_model,
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": SCRIPT_REWRITE_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": tts_prompt,
                    },
                ],
            )
            if not response.choices:
                last_error = RuntimeError("tts rewrite returned no choices")
                attempt_record["validation_error"] = str(last_error)
                prompt_log["rewrite"]["tts_attempts"].append(attempt_record)
                continue
            tts_script_text = _sanitize_prompt_text(
                _extract_completion_text(response.choices[0].message.content).strip()
            )
            attempt_record["response_text"] = tts_script_text
            attempt_record["char_count"] = _script_char_count(tts_script_text)
            if not tts_script_text:
                last_error = RuntimeError("tts rewrite returned empty content")
                attempt_record["validation_error"] = str(last_error)
                prompt_log["rewrite"]["tts_attempts"].append(attempt_record)
                continue
            try:
                _validate_tts_script(raw_report_text, tts_script_text)
                prompt_log["rewrite"]["tts_attempts"].append(attempt_record)
                prompt_log["rewrite"]["final"]["tts_script_text"] = tts_script_text
                prompt_log["rewrite"]["final"]["tts_prompt_final"] = attempt_record["prompt"]
                break
            except Exception as e:
                last_error = e
                attempt_record["validation_error"] = str(e)
                prompt_log["rewrite"]["tts_attempts"].append(attempt_record)
        else:
            raise RuntimeError(str(last_error or "tts rewrite failed after retries"))

        subtitle_script_text = _sanitize_prompt_text((seed_subtitle_script_text or "").strip())
        for attempt in range(max(1, max_attempts)):
            use_retry_prompt = attempt > 0 or bool(subtitle_script_text)
            subtitle_prompt = (
                build_subtitle_retry_prompt(
                    tts_script_text=tts_script_text,
                    previous_script_text=subtitle_script_text,
                    char_count=_script_char_count(subtitle_script_text),
                )
                if use_retry_prompt
                else build_subtitle_from_tts_prompt(tts_script_text=tts_script_text)
            )
            subtitle_prompt = _sanitize_prompt_text(subtitle_prompt)
            attempt_record = {
                "attempt": len(prompt_log["rewrite"]["subtitle_attempts"]) + 1,
                "prompt": subtitle_prompt,
                "response_text": "",
                "char_count": 0,
                "validation_error": "",
            }
            response = await client.chat.completions.create(
                model=settings.script_rewrite_model,
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": SCRIPT_REWRITE_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": subtitle_prompt,
                    },
                ],
            )
            if not response.choices:
                last_error = RuntimeError("subtitle rewrite returned no choices")
                attempt_record["validation_error"] = str(last_error)
                prompt_log["rewrite"]["subtitle_attempts"].append(attempt_record)
                continue
            subtitle_script_text = _sanitize_prompt_text(
                _extract_completion_text(response.choices[0].message.content).strip()
            )
            attempt_record["response_text"] = subtitle_script_text
            attempt_record["char_count"] = _script_char_count(subtitle_script_text)
            if not subtitle_script_text:
                last_error = RuntimeError("subtitle rewrite returned empty content")
                attempt_record["validation_error"] = str(last_error)
                prompt_log["rewrite"]["subtitle_attempts"].append(attempt_record)
                continue
            try:
                _validate_subtitle_script(tts_script_text, subtitle_script_text)
                prompt_log["rewrite"]["subtitle_attempts"].append(attempt_record)
                prompt_log["rewrite"]["final"] = {
                    "status": "success",
                    "error": "",
                    "tts_script_text": tts_script_text,
                    "subtitle_script_text": subtitle_script_text,
                    "tts_prompt_final": prompt_log["rewrite"]["tts_attempts"][-1]["prompt"] if prompt_log["rewrite"]["tts_attempts"] else "",
                    "subtitle_prompt_final": attempt_record["prompt"],
                }
                return subtitle_script_text, tts_script_text
            except Exception as e:
                last_error = e
                attempt_record["validation_error"] = str(e)
                prompt_log["rewrite"]["subtitle_attempts"].append(attempt_record)
    finally:
        await client.close()

    raise RuntimeError(str(last_error or "subtitle rewrite failed after retries"))


async def _prepare_report_delivery(
    *,
    job_id: str,
    raw_report_text: str,
    notebooklm_prompt: str,
    existing_job: Optional[dict],
    existing_script_json: object,
    filename: str,
    rewrite_prompt: str,
    manual_report_retry: bool = False,
    retry_notifier: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
) -> tuple[str, bytes, str, dict]:
    raw_report_text = (raw_report_text or "").strip()
    rewrite_prompt = (rewrite_prompt or "").strip()
    rewrite_instruction = build_tts_script_rewrite_instruction(rewrite_prompt)
    prompt_log = _build_job_prompt_log(
        job_id=job_id,
        existing_job=existing_job,
        notebooklm_prompt=notebooklm_prompt,
        raw_report_text=raw_report_text,
        rewrite_prompt=rewrite_prompt,
        rewrite_instruction=rewrite_instruction,
    )
    if not raw_report_text:
        prompt_log["rewrite"]["final"]["status"] = "failed"
        prompt_log["rewrite"]["final"]["error"] = "raw report is empty"
        _, merged_script = _upload_prompt_log_file(
            job_id=job_id,
            log_payload=prompt_log,
            existing_script_json=_merge_script_json_with_media_names(
                existing_script_json,
                report_filename=filename,
                notebooklm_report_text="",
                script_rewrite_prompt=rewrite_instruction,
                script_rewrite_status="failed",
                script_rewrite_error="raw report is empty",
            ),
        )
        raise ReportPreparationError("raw report is empty", script_json=merged_script)

    total_attempts = _REPORT_SCRIPT_RETRY_MAX_ATTEMPTS if manual_report_retry else 1
    stage_attempts = 1 if manual_report_retry else _SCRIPT_REWRITE_MAX_ATTEMPTS
    previous_tts_script_text = ""
    previous_subtitle_script_text = ""
    last_error: Exception | None = None

    for report_attempt_index in range(total_attempts):
        if report_attempt_index > 0 and retry_notifier is not None and last_error is not None:
            try:
                await retry_notifier(report_attempt_index + 1, total_attempts, str(last_error))
            except Exception as notify_error:
                logger.warning(
                    "[script-rewrite] retry notifier failed job_id=%s attempt=%d/%d: %s",
                    job_id,
                    report_attempt_index + 1,
                    total_attempts,
                    notify_error,
                )

        report_attempt_record = {
            "attempt": report_attempt_index + 1,
            "max_attempts": total_attempts,
            "status": "pending",
            "error": "",
        }
        prompt_log["rewrite"]["report_attempts"].append(report_attempt_record)

        try:
            subtitle_script_text, tts_script_text = await _rewrite_report_to_script(
                raw_report_text,
                rewrite_instruction,
                prompt_log=prompt_log,
                max_attempts=stage_attempts,
                seed_tts_script_text=previous_tts_script_text,
                seed_subtitle_script_text=previous_subtitle_script_text,
            )
            report_attempt_record["status"] = "success"
            rewrite_status = "success"
            rewrite_error = ""
            merged_script = _merge_script_json_with_media_names(
                existing_script_json,
                subtitle_script_text=subtitle_script_text,
                tts_script_text=tts_script_text,
                report_filename=filename,
                notebooklm_report_text=raw_report_text,
                script_rewrite_prompt=rewrite_instruction,
                script_rewrite_status=rewrite_status,
                script_rewrite_error=rewrite_error,
            )
            _, merged_script = _upload_prompt_log_file(
                job_id=job_id,
                log_payload=prompt_log,
                existing_script_json=merged_script,
            )
            return subtitle_script_text, subtitle_script_text.encode("utf-8"), tts_script_text, merged_script
        except Exception as e:
            last_error = e
            report_attempt_record["status"] = "failed"
            report_attempt_record["error"] = str(e)
            previous_tts_script_text = _latest_rewrite_response_text(prompt_log, "tts_attempts") or previous_tts_script_text
            previous_subtitle_script_text = _latest_rewrite_response_text(prompt_log, "subtitle_attempts") or previous_subtitle_script_text
            if report_attempt_index + 1 < total_attempts:
                logger.warning(
                    "[script-rewrite] retry scheduled job_id=%s attempt=%d/%d error=%s",
                    job_id,
                    report_attempt_index + 1,
                    total_attempts,
                    e,
                )
                continue
            logger.exception(
                "[script-rewrite] failed job_id=%s attempts=%d",
                job_id,
                report_attempt_index + 1,
            )
            failure_detail = (
                f"after {report_attempt_index + 1} attempts: {e}"
                if total_attempts > 1
                else str(e)
            )
            prompt_log["rewrite"]["final"]["status"] = "failed"
            prompt_log["rewrite"]["final"]["error"] = failure_detail
            failure_script = _merge_script_json_with_media_names(
                existing_script_json,
                report_filename=filename,
                notebooklm_report_text=raw_report_text,
                script_rewrite_prompt=rewrite_instruction,
                script_rewrite_status="failed",
                script_rewrite_error=failure_detail,
            )
            _, failure_script = _upload_prompt_log_file(
                job_id=job_id,
                log_payload=prompt_log,
                existing_script_json=failure_script,
            )
            raise ReportPreparationError(f"script rewrite failed: {failure_detail}", script_json=failure_script) from e

    raise ReportPreparationError("script rewrite failed: unknown error")


def _upload_tts_script_file(
    *,
    job_id: str,
    filename: str,
    tts_script_text: str,
    existing_script_json: object,
) -> tuple[object | None, dict]:
    # 사용자에게 노출되는 subtitle 파일과 별개로,
    # TTS용 스크립트도 별도 S3 prefix(scripts/)에 보관한다.
    normalized_filename = _normalize_report_filename(job_id, filename, existing_script_json)
    if not tts_script_text.strip():
        return None, _merge_script_json_with_media_names(
            existing_script_json,
            tts_script_filename=normalized_filename,
            tts_script_s3_uri="",
        )
    stored = put_bytes_and_presign(
        prefix=settings.media_s3_prefix_scripts,
        filename=normalized_filename,
        content=tts_script_text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
    )
    merged_script = _merge_script_json_with_media_names(
        existing_script_json,
        tts_script_filename=normalized_filename,
        tts_script_s3_uri=stored.s3_uri,
    )
    logger.info("[storage] tts script uploaded job_id=%s filename=%s s3_uri=%s", job_id, normalized_filename, stored.s3_uri)
    return stored, merged_script


async def _generate_tts_audio_content(job_id: str, script_text: str) -> tuple[str, str]:
    # gateway가 직접 TTS API를 호출해 WAV 바이트를 받아오고,
    # 이후 Discord/S3 저장은 send_audio 엔드포인트에 위임한다.
    tts_api_url = settings.tts_api_url.strip()
    if not tts_api_url:
        raise RuntimeError("TTS_API_URL is not configured")

    tts_body = _build_tts_request_body(script_text)

    async with httpx.AsyncClient(timeout=180.0) as client:
        endpoint = tts_api_url if tts_api_url.rstrip("/").endswith("/tts") else f"{tts_api_url.rstrip('/')}/tts"
        resp = await client.post(
            endpoint,
            json=tts_body,
            headers={"Content-Type": "application/json"},
        )
        if not resp.is_success:
            raise RuntimeError(_format_tts_api_error(resp.status_code, resp.text))
        audio_b64 = base64.b64encode(resp.content).decode("ascii")

    filename = _normalize_audio_filename(job_id, None, None)
    return audio_b64, filename


async def _run_tts_generation(
    *,
    job_id: str,
    script_text: str,
    channel_id: str,
    user_id: str,
    auto_trigger_wf12: bool,
) -> None:
    try:
        # 1) TTS 생성 2) Discord/S3 저장 3) 필요 시 WF-12 자동 트리거 순서로 진행한다.
        await job_service.transition_status(job_id, "GENERATING")
        audio_b64, filename = await _generate_tts_audio_content(job_id, script_text)
        send_result = await send_audio(
            None,
            SendAudioRequest(
                messenger_source=MessengerSource.DISCORD,
                messenger_channel_id=channel_id,
                job_id=job_id,
                audio_content_b64=audio_b64,
                audio_file_path="",
                filename=filename,
                caption=_default_tts_caption(auto_trigger_wf12=auto_trigger_wf12),
                include_wf12_button=not auto_trigger_wf12,
            ),
        )
        await job_service.transition_status(job_id, "APPROVED")
        current_job = await job_service.get_job(job_id)
        cleared_script = _merge_script_json_with_media_names(
            current_job.get("script_json") if current_job else None,
            tts_error_type="",
            tts_error_detail="",
        )
        await job_service.update_job(job_id, error_message="", script_json=cleared_script)

        if auto_trigger_wf12:
            # report_to_video 경로는 TTS 승인 버튼을 기다리지 않고
            # 생성된 audio_url을 바로 WF-12 입력으로 넘긴다.
            job = await job_service.get_job(job_id)
            if job is None:
                raise RuntimeError(f"Job not found while resolving HeyGen avatar: {job_id}")
            resolved_avatar_id, avatar_source = await _resolve_heygen_avatar_id(job)
            approved_audio_url = str(send_result.get("audio_url") or "").strip()
            if not approved_audio_url:
                raise RuntimeError("Generated TTS audio_url is empty")
            await n8n_service.call_wf12_heygen_generate(
                job_id,
                channel_id,
                user_id,
                audio_url=approved_audio_url,
                avatar_id=resolved_avatar_id,
                use_avatar_iv_model=False,
            )
            logger.info("[heygen-avatar] auto wf12 avatar source=%s job_id=%s", avatar_source, job_id)
    except Exception as e:
        logger.exception("[tts-direct] failed job_id=%s: %r", job_id, e)
        error_text = str(e)
        error_type = _extract_tts_error_type(error_text)
        await job_service.transition_status(job_id, "FAILED")
        current_job = await job_service.get_job(job_id)
        failed_script = _merge_script_json_with_media_names(
            current_job.get("script_json") if current_job else None,
            tts_error_type=error_type,
            tts_error_detail=error_text,
        )
        await job_service.update_job(
            job_id,
            error_message=f"TTS 생성 실패 [{error_type}]: {error_text}",
            script_json=failed_script,
        )
        try:
            await _discord_adapter.send_text_message(
                channel_id,
                f"❌ TTS 생성에 실패했습니다.\nJob ID: {job_id[:8]}\n오류 유형: {error_type}\n오류: {error_text}",
            )
        except Exception as notify_err:
            logger.error("[tts-direct] failure notify failed job_id=%s: %s", job_id, notify_err)


def _spawn_tts_generation(
    *,
    job_id: str,
    script_text: str,
    channel_id: str,
    user_id: str,
    auto_trigger_wf12: bool,
) -> None:
    # slash command/버튼 응답을 오래 붙잡지 않기 위해
    # 실제 TTS 생성은 background task로 분리한다.
    asyncio.create_task(
        _run_tts_generation(
            job_id=job_id,
            script_text=script_text,
            channel_id=channel_id,
            user_id=user_id,
            auto_trigger_wf12=auto_trigger_wf12,
        )
    )


async def _create_prompt_tts_job(body: ManualGenerateRequest) -> dict:
    normalized_prompt = (body.prompt or "").strip()
    messenger_user_id = (body.messenger_user_id or "").strip()
    messenger_channel_id = (body.messenger_channel_id or "").strip()
    if not normalized_prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if not messenger_user_id or not messenger_channel_id:
        raise HTTPException(
            status_code=400,
            detail="messenger_user_id and messenger_channel_id are required when prompt is provided",
        )

    job_id = _normalize_manual_job_id(body.job_id) or str(uuid.uuid4())
    request = IncomingMessageRequest(
        job_id=job_id,
        messenger_source=MessengerSource.DISCORD,
        messenger_user_id=messenger_user_id,
        messenger_channel_id=messenger_channel_id,
        concept_text=normalized_prompt,
        character_id="default-character",
    )
    created_job = await job_service.create_job(request)
    script_json = _merge_script_json_with_media_names(
        created_job.get("script_json"),
        script_text=normalized_prompt,
        subtitle_script_text=normalized_prompt,
        tts_script_text=normalized_prompt,
    )
    script_json["raw_prompt_text"] = normalized_prompt
    script_json["script_source_type"] = "direct_prompt"
    return await job_service.update_job(job_id, error_message="", script_json=script_json)


async def _post_notebooklm_with_retry(
    endpoint: str,
    payload: dict,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.5,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await _http_client.post(
                f"{settings.notebooklm_service_url}{endpoint}",
                json=payload,
                headers={"X-Internal-Secret": settings.gateway_internal_secret},
                timeout=timeout_seconds,
            )
            return resp
        except Exception as e:
            last_error = e
            is_transient = _is_transient_notebooklm_error(e)
            if attempt >= max_attempts or not is_transient:
                raise
            wait = backoff_seconds * attempt
            logger.warning(
                "[notebooklm] transient request failure endpoint=%s attempt=%d/%d wait=%.1fs err=%s",
                endpoint,
                attempt,
                max_attempts,
                wait,
                e,
            )
            await asyncio.sleep(wait)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"unexpected notebooklm request failure: {endpoint}")


async def _get_notebook_state_payload(channel_id: str) -> dict:
    resp = await _post_notebooklm_with_retry(
        "/notebook-state",
        {"channel_id": channel_id},
        timeout_seconds=30.0,
        max_attempts=3,
        backoff_seconds=1.5,
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Notebook state request failed: HTTP {resp.status_code}",
        )
    payload = resp.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Notebook state returned invalid payload")
    if payload.get("status") != "success":
        raise HTTPException(
            status_code=502,
            detail=f"Notebook state error: {payload.get('error') or 'unknown'}",
        )
    return payload


async def _resolve_manual_job(
    body: ManualGenerateRequest,
    *,
    require_script: bool = False,
    require_audio: bool = False,
) -> dict:
    user_id = (body.messenger_user_id or "").strip()
    channel_id = (body.messenger_channel_id or "").strip()
    normalized_job_id = _normalize_manual_job_id(body.job_id)
    if not user_id or not channel_id:
        if not normalized_job_id:
            raise HTTPException(
                status_code=400,
                detail="job_id is required when messenger_user_id/channel_id are missing",
            )
        exact = await job_service.get_job(normalized_job_id)
        if exact is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return exact

    if not normalized_job_id:
        latest = await job_service.get_latest_job(
            user_id,
            channel_id,
            require_script=require_script,
            require_audio=require_audio,
        )
        if latest is None:
            requirement = "script_text" if require_script else "audio_url" if require_audio else "조건"
            raise HTTPException(
                status_code=404,
                detail=f"No recent jobs found for this user/channel with required {requirement}",
            )
        resolved_job = await job_service.get_job(latest["id"])
        if resolved_job is None:
            raise HTTPException(status_code=404, detail="Resolved latest job no longer exists")
        return resolved_job

    exact = await job_service.get_job(normalized_job_id)
    if exact is not None:
        if exact.get("messenger_user_id") != user_id or exact.get("messenger_channel_id") != channel_id:
            raise HTTPException(status_code=403, detail="Job belongs to a different user/channel")
        return exact

    matches = await job_service.find_jobs_by_prefix(
        normalized_job_id,
        user_id,
        channel_id,
        require_script=require_script,
        require_audio=require_audio,
    )
    if len(matches) == 1:
        resolved_job = await job_service.get_job(matches[0]["id"])
        if resolved_job is None:
            raise HTTPException(status_code=404, detail="Resolved job no longer exists")
        return resolved_job

    if len(matches) > 1:
        items = ", ".join(_format_job_summary(m) for m in matches[:5])
        raise HTTPException(
            status_code=409,
            detail=f"Ambiguous job_id prefix. Matches: {items}",
        )

    raise HTTPException(status_code=404, detail="Job not found for this user/channel")


def _parse_topic_channels(raw: str) -> list[dict]:
    """TOPIC_CHANNELS(채널명/채널ID+...)를 버튼용 채널 목록으로 파싱."""
    channels: list[dict] = []
    seen: set[str] = set()
    for chunk in (raw or "").split("+"):
        part = chunk.strip()
        if not part:
            continue
        slash_idx = part.find("/")
        if slash_idx < 0:
            continue
        name = part[:slash_idx].strip()
        cid = part[slash_idx + 1 :].strip()
        if not name or not cid:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        channels.append({"id": cid, "name": name})
    return channels


def _parse_csv_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _get_primary_discord_channel_id() -> str:
    channel_ids = _parse_csv_ids(settings.discord_allowed_channel_ids)
    if not channel_ids:
        raise HTTPException(
            status_code=400,
            detail="DISCORD_ALLOWED_CHANNEL_IDS is empty; cannot route auto report",
        )
    return channel_ids[0]


def _build_auto_report_prompt(body: AutoReportRequest) -> str:
    # WF-09 자동 수집 경로에서는 최신 영상 메타데이터만 붙여
    # "오늘 보고서"용 최소 프롬프트를 만든다.
    segments: list[str] = []
    if body.channel_name:
        segments.append(f"대상 채널명: {body.channel_name}.")
    if body.source_title:
        segments.append(f"최신 참고 영상 제목: {body.source_title}.")
    if body.source_url:
        segments.append(f"최신 참고 영상 URL: {body.source_url}.")
    segments.append("위 최신 소스를 참고해 오늘 업로드 흐름에 맞는 대사를 작성한다.")
    return " ".join(segments)


def _should_skip_auto_report_discord_delivery(job: dict | None) -> bool:
    # 시간별 자동 보고서는 S3/DB 적재까지만 두고,
    # Discord 노출은 설정으로 별도 제어한다.
    if settings.auto_report_discord_delivery_enabled:
        return False
    if not job:
        return False
    return (job.get("messenger_user_id") or "").strip() == "system:auto-report"


def _launch_bg_task(coro: Awaitable[None], *, task_name: str, job_id: str) -> None:
    # background task 예외는 호출 스택 밖으로 사라지므로
    # done callback에서 반드시 로그로 다시 끌어올린다.
    task = asyncio.create_task(coro)

    def _done_callback(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except Exception:
            logger.exception("[%s] background task failed job_id=%s", task_name, job_id)

    task.add_done_callback(_done_callback)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _discord_adapter

    await job_service.get_db_pool()

    _http_client = httpx.AsyncClient(timeout=10.0)
    _discord_adapter = DiscordAdapter(token=settings.discord_bot_token, http_client=_http_client)

    logger.info("messenger-gateway started (discord adapter ready)")
    yield

    await job_service.close_db_pool()
    await n8n_service.close_http_client()
    if _http_client:
        await _http_client.aclose()
    logger.info("messenger-gateway shutdown")


app = FastAPI(title="Messenger Gateway", lifespan=lifespan)


# ─────────────────────────────────────────
# 인증 의존성
# ─────────────────────────────────────────

async def verify_secret(x_internal_secret: Annotated[Optional[str], Header()] = None) -> None:
    if x_internal_secret != settings.gateway_internal_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Internal-Secret header",
        )


AuthDep = Annotated[None, Depends(verify_secret)]


# ─────────────────────────────────────────
# 어댑터 반환 헬퍼
# ─────────────────────────────────────────

def get_adapter(messenger_source: str) -> DiscordAdapter:
    if messenger_source == "discord":
        return _discord_adapter
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown messenger_source: {messenger_source}",
    )


# ─────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────

@app.post("/internal/message")
async def receive_message(_: AuthDep, body: IncomingMessageRequest) -> dict:
    """봇 서비스가 포워딩하는 사용자 메시지를 수신한다."""
    # /create 요청의 진입점: DB에 job을 만들고, 실제 스크립트 생성은 WF-01로 넘긴다.
    await job_service.create_job(body)

    try:
        await n8n_service.call_wf01_input(
            job_id=body.job_id,
            messenger_source=body.messenger_source.value,
            messenger_user_id=body.messenger_user_id,
            messenger_channel_id=body.messenger_channel_id,
            concept_text=body.concept_text,
            ref_image_url=body.ref_image_url,
            character_id=body.character_id,
        )
    except Exception as e:
        logger.error("[discord] call_wf01_input failed job_id=%s: %s", body.job_id, e)
        await job_service.update_job(body.job_id, error_message=str(e))

    return {"job_id": body.job_id, "status": "accepted"}


@app.post("/internal/send-confirm")
async def send_confirm(_: AuthDep, body: SendConfirmRequest) -> dict:
    """n8n WF-04에서 호출 — Discord로 컨펌 메시지를 전송한다."""
    # Discord 버튼 메시지 id를 DB에 저장해 두어야 이후 승인/수정 시 버튼 제거가 가능하다.
    try:
        confirm_message_id = await _discord_adapter.send_confirm_message(
            channel_id=body.messenger_channel_id,
            user_id=body.messenger_user_id,
            job_id=body.job_id,
            title=body.title,
            script_summary=body.script_summary,
            preview_url=body.preview_url,
        )
    except Exception as e:
        logger.error("[discord] send_confirm_message failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    await job_service.update_job(body.job_id, confirm_message_id=confirm_message_id)
    await job_service.transition_status(body.job_id, "WAITING_APPROVAL")

    logger.info("[discord] send_confirm done job_id=%s", body.job_id)
    return {"job_id": body.job_id, "confirm_message_id": confirm_message_id}


@app.post("/internal/confirm-action")
async def confirm_action(_: AuthDep, body: ConfirmActionRequest) -> dict:
    """봇 서비스가 버튼 클릭 이벤트를 포워딩한다."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] in ("APPROVED", "PUBLISHED", "PUBLISHING"):
        raise HTTPException(status_code=409, detail=f"Job already in status: {job['status']}")

    channel_id = job["messenger_channel_id"]
    confirm_message_id = job.get("confirm_message_id")

    if body.action == "approved":
        # 실제 후속 처리(WF-05)가 성공한 뒤에만 Discord 쪽 성공 메시지를 보낸다.
        try:
            await n8n_service.call_wf05_confirm(body.job_id, "approved")
        except Exception as e:
            logger.error("call_wf05_confirm failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=502, detail=f"WF-05 trigger failed: {e}")

        if confirm_message_id:
            try:
                await _discord_adapter.remove_buttons(channel_id, confirm_message_id, "✅ 승인됨")
            except Exception as e:
                logger.error("[discord] remove_buttons failed job_id=%s: %s", body.job_id, e)

        try:
            await _discord_adapter.send_text_message(channel_id, "🎬 승인되었습니다! TTS 및 영상 생성을 시작합니다. (약 5~10분 소요)")
        except Exception as e:
            logger.error("[discord] send_text_message failed job_id=%s: %s", body.job_id, e)

        logger.info("[discord] confirm_action=approved job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "approved"}

    elif body.action == "revision_requested":
        if not body.revision_note:
            # 첫 클릭 시에는 note를 아직 모르므로 "다음 사용자 메시지 대기" 상태만 기록한다.
            await job_service.update_job(body.job_id, status="REVISION_REQUESTED")
            logger.info("[discord] confirm_action=revision_requested (pending note) job_id=%s", body.job_id)
            return {"job_id": body.job_id, "action": "revision_requested", "pending_note": True}

        try:
            await n8n_service.call_wf05_confirm(body.job_id, "revision_requested", body.revision_note)
        except Exception as e:
            logger.error("call_wf05_confirm failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=502, detail=f"WF-05 trigger failed: {e}")

        if confirm_message_id:
            try:
                await _discord_adapter.remove_buttons(channel_id, confirm_message_id, "✏️ 수정 요청됨")
            except Exception as e:
                logger.error("[discord] remove_buttons failed job_id=%s: %s", body.job_id, e)

        try:
            await _discord_adapter.send_text_message(channel_id, "🔄 수정 요청이 접수되었습니다. 재작업을 시작합니다.")
        except Exception as e:
            logger.error("[discord] send_text_message failed job_id=%s: %s", body.job_id, e)

        logger.info("[discord] confirm_action=revision_requested job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "revision_requested"}

    raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


@app.post("/internal/report-message")
async def report_message(_: AuthDep, body: ReportMessageRequest) -> dict:
    """봇 서비스가 report: 프리픽스 메시지를 포워딩한다."""
    await job_service.create_job(body)
    # /report는 응답 속도를 위해 즉시 반환하고,
    # background task에서 채널 선택/기존 보고서 조회/WF-06 분기를 처리한다.
    asyncio.create_task(_handle_report_message_bg(body))
    return {"job_id": body.job_id, "status": "accepted"}


@app.post("/internal/auto-report")
async def auto_report(_: AuthDep, body: AutoReportRequest) -> dict:
    """WF-09 소스 추가 성공 후 자동으로 WF-06 보고서 생성을 트리거한다."""
    # 자동 수집 경로는 Discord 사용자 대신 system:auto-report 계정으로 job을 만든다.
    notebook_state = await _get_notebook_state_payload(body.channel_id)
    notebook_url = str(notebook_state.get("notebook_url") or "").strip()
    has_reports = bool(notebook_state.get("has_reports"))
    if not notebook_url:
        raise HTTPException(status_code=502, detail="Notebook state returned empty notebook_url")
    if has_reports:
        logger.info(
            "[auto-report] skip channel=%s notebook=%s reason=existing-report",
            body.channel_id,
            notebook_url,
        )
        return {
            "status": "skipped",
            "reason": "existing-report",
            "notebook_url": notebook_url,
        }

    existing_auto_job = await job_service.find_existing_auto_report_job(
        channel_id=body.channel_id,
        notebook_url=notebook_url,
    )
    if existing_auto_job is not None:
        logger.info(
            "[auto-report] skip channel=%s notebook=%s reason=existing-job job_id=%s status=%s",
            body.channel_id,
            notebook_url,
            existing_auto_job["id"],
            existing_auto_job.get("status", ""),
        )
        return {
            "status": "skipped",
            "reason": "existing-job",
            "job_id": existing_auto_job["id"],
            "job_status": existing_auto_job.get("status", ""),
            "notebook_url": notebook_url,
        }

    target_channel_id = _get_primary_discord_channel_id()
    prompt = _build_auto_report_prompt(body)
    job_id = str(uuid.uuid4())

    report_body = ReportMessageRequest(
        job_id=job_id,
        messenger_source=MessengerSource.DISCORD,
        messenger_user_id="system:auto-report",
        messenger_channel_id=target_channel_id,
        prompt=prompt,
        notebook_id="",
        channel_id=body.channel_id,
        character_id="default-character",
    )

    await job_service.create_job(report_body)
    await job_service.update_job(
        job_id,
        script_json={
            "auto_report_channel_id": body.channel_id,
            "auto_report_notebook_url": notebook_url,
            "auto_report_source_url": body.source_url,
            "auto_report_source_title": body.source_title,
        },
    )
    wf06_ok = await _call_wf06(report_body, raise_on_error=True)
    if not wf06_ok:
        raise HTTPException(status_code=500, detail="WF-06 trigger failed")
    logger.info(
        "[auto-report] triggered job_id=%s discord_channel=%s youtube_channel=%s source=%s",
        job_id,
        target_channel_id,
        body.channel_id,
        body.source_url,
    )
    return {
        "status": "triggered",
        "job_id": job_id,
        "messenger_channel_id": target_channel_id,
        "youtube_channel_id": body.channel_id,
    }


async def _get_all_channels() -> list[dict]:
    """채널 버튼 목록은 TOPIC_CHANNELS만 사용한다."""
    channels = _parse_topic_channels(settings.topic_channels)
    if not channels:
        logger.warning("[report-message] TOPIC_CHANNELS 파싱 결과가 비어 있음")
    return channels


async def _handle_report_message_bg(body: ReportMessageRequest) -> None:
    """채널 선택 버튼 표시."""
    channels = await _get_all_channels()
    if channels:
        try:
            await _discord_adapter.send_channel_list(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                channels=channels,
            )
        except Exception as e:
            logger.error("[report-message] send_channel_list failed job_id=%s: %s", body.job_id, e)
        return

    # 채널 없으면 WF-06 직행
    await _call_wf06(body)


async def _handle_channel_selected_bg(body: ReportMessageRequest) -> None:
    """채널 선택 후 → 기존 보고서 목록 조회 or WF-06."""
    logger.info(
        "[channel-select:bg] start job_id=%s channel_id=%s",
        body.job_id,
        body.channel_id,
    )
    try:
        await _discord_adapter.send_text_message(
            body.messenger_channel_id,
            "🔎 채널을 확인했습니다. 기존 보고서 목록을 조회 중입니다... (최대 3분)",
        )
    except Exception as e:
        logger.warning("[channel-select:bg] start notice failed job_id=%s: %s", body.job_id, e)

    reports: list[str] = []
    list_reports_error: str = ""
    list_reports_failed = False
    try:
        resp = await _post_notebooklm_with_retry(
            "/list-reports",
            {
                "notebook_id": body.notebook_id or None,
                "channel_id": body.channel_id or None,
            },
            timeout_seconds=180.0,
            max_attempts=3,
            backoff_seconds=1.5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                reports = data.get("reports", [])
            else:
                list_reports_failed = True
                list_reports_error = str(data.get("error") or "status!=success")
                logger.warning(
                    "[channel-select:bg] list-reports status!=success job_id=%s error=%s",
                    body.job_id,
                    data.get("error"),
                )
        else:
            list_reports_failed = True
            list_reports_error = f"HTTP {resp.status_code}"
            logger.warning(
                "[channel-select:bg] list-reports non-200 job_id=%s status=%s body=%s",
                body.job_id,
                resp.status_code,
                (resp.text or "")[:300],
            )
    except Exception as e:
        list_reports_failed = True
        if _is_transient_notebooklm_error(e):
            list_reports_error = "일시적인 네트워크/DNS 문제로 NotebookLM 연결에 실패했습니다."
        else:
            list_reports_error = str(e)
        logger.warning("[report-message] list-reports 조회 실패: %s", e)

    if reports:
        try:
            await _discord_adapter.send_report_list(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                reports=reports,
                selected_channel_id=body.channel_id,
            )
            logger.info("[channel-select:bg] reports listed job_id=%s count=%d", body.job_id, len(reports))
            return
        except Exception as e:
            logger.error("[report-message] send_report_list failed job_id=%s: %s", body.job_id, e)
            list_reports_failed = True
            list_reports_error = f"send_report_list failed: {e}"

    # 자동 fallback으로 바로 WF-06 실행하지 않고, 사용자 선택 버튼(다시 조회/새로 생성)을 제공한다.
    if list_reports_failed:
        reason_text = (
            "⚠️ 기존 보고서 목록 조회에 실패했습니다.\n"
            f"사유: {list_reports_error[:160]}\n"
            "아래에서 `다시 조회` 또는 `새로 생성`을 선택해주세요."
        )
        try:
            await _discord_adapter.send_report_recovery_actions(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                selected_channel_id=body.channel_id,
                reason_text=reason_text,
                include_retry=True,
            )
        except Exception as e:
            logger.warning("[channel-select:bg] recovery action send failed job_id=%s: %s", body.job_id, e)
        return

    # 정상적으로 성공 응답이지만 목록이 비어 있는 경우
    try:
        await _discord_adapter.send_report_recovery_actions(
            channel_id=body.messenger_channel_id,
            job_id=body.job_id,
            selected_channel_id=body.channel_id,
            reason_text=(
                "📄 해당 채널의 기존 보고서를 찾지 못했습니다.\n"
                "아래에서 `새로 생성`을 선택해 보고서를 만들 수 있습니다."
            ),
            include_retry=False,
        )
    except Exception as e:
        logger.warning("[channel-select:bg] empty-list action send failed job_id=%s: %s", body.job_id, e)


async def _call_wf06(body: ReportMessageRequest, *, raise_on_error: bool = False) -> bool:
    logger.info(
        "[report-wf06] trigger job_id=%s channel_id=%s messenger_channel=%s",
        body.job_id,
        body.channel_id,
        body.messenger_channel_id,
    )
    try:
        await n8n_service.call_wf06_report(
            job_id=body.job_id,
            messenger_source=body.messenger_source.value,
            messenger_user_id=body.messenger_user_id,
            messenger_channel_id=body.messenger_channel_id,
            prompt=NOTEBOOKLM_REPORT_PROMPT,
            notebook_id=body.notebook_id,
            channel_id=body.channel_id,
            character_id=body.character_id,
        )
        return True
    except Exception as e:
        logger.error("[discord] call_wf06_report failed job_id=%s: %s", body.job_id, e)
        await job_service.update_job(body.job_id, error_message=str(e))
        try:
            await _discord_adapter.send_text_message(
                body.messenger_channel_id,
                f"❌ 보고서 생성 요청 전송 실패(job `{body.job_id[:8]}`): {str(e)[:180]}",
            )
        except Exception:
            logger.warning("[report-wf06] failed to notify channel for wf06 error job_id=%s", body.job_id)
        if raise_on_error:
            raise
    return False


@app.post("/internal/channel-select")
async def channel_select(_: AuthDep, body: ChannelSelectRequest) -> dict:
    """Discord 채널 선택 버튼 클릭 → 해당 채널의 보고서 목록 조회."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    report_body = ReportMessageRequest(
        job_id=body.job_id,
        messenger_source=job["messenger_source"],
        messenger_user_id=job["messenger_user_id"],
        messenger_channel_id=job["messenger_channel_id"],
        prompt=job.get("concept_text", ""),
        notebook_id="",
        channel_id=body.channel_id,
        character_id=job.get("character_id", "default-character"),
    )
    _launch_bg_task(_handle_channel_selected_bg(report_body), task_name="channel-select", job_id=body.job_id)
    logger.info("[channel-select] job_id=%s channel_id=%s", body.job_id, body.channel_id)
    return {"job_id": body.job_id, "status": "accepted"}


@app.post("/internal/report-select")
async def report_select(_: AuthDep, body: ReportSelectRequest) -> dict:
    """Discord 보고서 선택/새로 생성 버튼 클릭을 처리한다."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if body.action not in ("select", "new"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")
    if body.action == "select" and body.report_index is None:
        raise HTTPException(status_code=400, detail="report_index is required for action=select")

    # 즉시 반환 후 background에서 처리 (get-report는 최대 300s 소요)
    _launch_bg_task(_handle_report_select_bg(body, job), task_name="report-select", job_id=body.job_id)
    return {"job_id": body.job_id, "status": "accepted"}


async def _handle_report_select_bg(body: ReportSelectRequest, job: dict) -> None:
    channel_id = job["messenger_channel_id"]

    if body.action == "select":
        # 진행 중 안내 메시지
        try:
            await _discord_adapter.send_text_message(channel_id, "📄 보고서를 가져오는 중입니다... (10~30초 소요)")
        except Exception:
            pass

        try:
            resp = await _http_client.post(
                f"{settings.notebooklm_service_url}/get-report",
                json={
                    "job_id": body.job_id,
                    "channel_id": body.channel_id or None,
                    "report_index": body.report_index,
                },
                headers={"X-Internal-Secret": settings.gateway_internal_secret},
                timeout=300.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("[report-select] get-report failed job_id=%s: %s", body.job_id, e)
            await _discord_adapter.send_text_message(channel_id, f"❌ 보고서 가져오기 실패: {e}")
            return

        if data.get("status") != "success":
            err = data.get("error", "get-report 실패")
            logger.error("[report-select] get-report error job_id=%s: %s", body.job_id, err)
            await _discord_adapter.send_text_message(channel_id, f"❌ 보고서 가져오기 실패: {err}")
            return

        report_content = data["report_content"]
        filename = _normalize_report_filename(body.job_id, data.get("filename"), job.get("script_json"))
        async def _notify_report_retry(attempt: int, max_attempts: int, reason: str) -> None:
            await _discord_adapter.send_text_message(
                channel_id,
                _build_report_retry_notice(
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=reason,
                ),
            )
        try:
            final_script_text, file_bytes, tts_script_text, merged_script = await _prepare_report_delivery(
                job_id=body.job_id,
                raw_report_text=report_content,
                notebooklm_prompt="",
                existing_job=job,
                existing_script_json=job.get("script_json"),
                filename=filename,
                rewrite_prompt=job.get("concept_text", ""),
                manual_report_retry=True,
                retry_notifier=_notify_report_retry,
            )
        except ReportPreparationError as e:
            update_kwargs = {"error_message": str(e)}
            if e.script_json:
                update_kwargs["script_json"] = e.script_json
            await job_service.update_job(body.job_id, **update_kwargs)
            await _discord_adapter.send_text_message(channel_id, f"❌ 대본 생성 실패: {str(e)[:180]}")
            return
        except Exception as e:
            await job_service.update_job(body.job_id, error_message=str(e))
            await _discord_adapter.send_text_message(channel_id, f"❌ 대본 생성 실패: {str(e)[:180]}")
            return
        try:
            _, merged_script = _upload_tts_script_file(
                job_id=body.job_id,
                filename=filename,
                tts_script_text=tts_script_text,
                existing_script_json=merged_script,
            )
        except Exception as e:
            logger.error("[storage] tts script upload failed job_id=%s: %s", body.job_id, e)
        await job_service.update_job(
            body.job_id,
            script_json=merged_script,
        )

        text = final_script_text
        if len(text) > 1800:
            text = text[:1800] + "\n\n[전체 내용은 첨부 파일 참조]"

        try:
            await _discord_adapter.send_file_message(
                channel_id=channel_id,
                text=text,
                file_bytes=file_bytes,
                filename=filename,
                include_tts_button=True,
                include_video_button=True,
                job_id=body.job_id,
            )
        except Exception as e:
            logger.error("[report-select] send_file_message failed job_id=%s: %s", body.job_id, e)
            return

        await job_service.transition_status(body.job_id, "PUBLISHED")
        logger.info("[report-select] action=select done job_id=%s index=%d", body.job_id, body.report_index)

    elif body.action == "new":
        try:
            await _discord_adapter.send_text_message(channel_id, "🆕 새 보고서를 생성합니다... (최대 5분 소요)")
        except Exception:
            pass

        report_msg = ReportMessageRequest(
            job_id=job["id"],
            messenger_source=job["messenger_source"],
            messenger_user_id=job["messenger_user_id"],
            messenger_channel_id=channel_id,
            prompt=job.get("concept_text", ""),
            notebook_id="",
            channel_id=body.channel_id,
            character_id=job.get("character_id", "default-character"),
        )
        await _call_wf06(report_msg)
        logger.info("[report-select] action=new job_id=%s → WF-06 triggered", body.job_id)


@app.post("/internal/send-report")
async def send_report(_: AuthDep, body: SendReportRequest) -> dict:
    """n8n WF-06에서 호출 — Discord로 보고서 파일을 전송한다."""
    # 여기서 raw report -> subtitle/tts 스크립트 분리, S3 저장, Discord 전송을 한 번에 마무리한다.
    existing_job = await job_service.get_job(body.job_id)
    manual_report_retry = existing_job is not None and not _should_skip_auto_report_discord_delivery(existing_job)
    filename = _normalize_report_filename(
        body.job_id,
        body.filename,
        existing_job.get("script_json") if existing_job else None,
    )
    async def _notify_report_retry(attempt: int, max_attempts: int, reason: str) -> None:
        await _discord_adapter.send_text_message(
            body.messenger_channel_id,
            _build_report_retry_notice(
                attempt=attempt,
                max_attempts=max_attempts,
                reason=reason,
            ),
        )
    try:
        final_script_text, file_bytes, tts_script_text, merged_script = await _prepare_report_delivery(
            job_id=body.job_id,
            raw_report_text=body.report_content,
            notebooklm_prompt=NOTEBOOKLM_REPORT_PROMPT,
            existing_job=existing_job,
            existing_script_json=existing_job.get("script_json") if existing_job else None,
            filename=filename,
            rewrite_prompt=existing_job.get("concept_text", "") if existing_job else "",
            manual_report_retry=manual_report_retry,
            retry_notifier=_notify_report_retry if manual_report_retry else None,
        )
    except ReportPreparationError as e:
        update_kwargs = {"error_message": str(e)}
        if e.script_json:
            update_kwargs["script_json"] = e.script_json
        await job_service.update_job(body.job_id, **update_kwargs)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        await job_service.update_job(body.job_id, error_message=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    try:
        # TTS용 파일 업로드 실패는 사용자 보고서 전송을 막지 않도록 분리 처리한다.
        _, merged_script = _upload_tts_script_file(
            job_id=body.job_id,
            filename=filename,
            tts_script_text=tts_script_text,
            existing_script_json=merged_script,
        )
    except Exception as e:
        logger.error("[storage] tts script upload failed job_id=%s: %s", body.job_id, e)

    stored = None
    upload_error = None
    try:
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_reports,
            filename=filename,
            content=file_bytes,
            content_type="text/plain; charset=utf-8",
        )
    except Exception as e:
        upload_error = e
        logger.error("[storage] report upload failed job_id=%s: %s", body.job_id, e)

    attachment_limit = _discord_attachment_limit_bytes()
    is_link_only_report = stored.size_bytes > attachment_limit if stored else len(file_bytes) > attachment_limit
    # Discord 첨부 제한을 넘으면 링크 메시지로 전환하고, 그마저 업로드가 없으면 에러로 본다.
    if upload_error is not None and is_link_only_report:
        raise HTTPException(status_code=500, detail=f"Report upload failed: {upload_error}")

    text = final_script_text
    if len(text) > 1800:
        overflow_hint = (
            "[전체 내용은 아래 링크 참조]"
            if is_link_only_report
            else "[전체 내용은 첨부 파일 참조]"
        )
        text = text[:1800] + f"\n\n{overflow_hint}"

    report_storage_url = stored.s3_uri if stored else ""
    await job_service.update_job(
        body.job_id,
        final_url=report_storage_url or None,
        script_json=merged_script,
    )

    if _should_skip_auto_report_discord_delivery(existing_job):
        logger.info(
            "[discord] send_report skipped for auto-report job_id=%s filename=%s",
            body.job_id,
            filename,
        )
        return {
            "status": "stored_only",
            "filename": filename,
            "file_url": stored.presigned_url if stored else "",
            "s3_uri": report_storage_url,
        }

    if is_link_only_report:
        try:
            await _discord_adapter.send_report_link_message(
                channel_id=body.messenger_channel_id,
                text=text,
                report_url=stored.presigned_url if stored else "",
                include_tts_button=body.include_tts_button,
                include_video_button=body.include_video_button,
                job_id=body.job_id,
            )
        except Exception as e:
            logger.error("[discord] send_report_link_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))
    else:
        try:
            await _discord_adapter.send_file_message(
                channel_id=body.messenger_channel_id,
                text=text,
                file_bytes=file_bytes,
                filename=filename,
                include_tts_button=body.include_tts_button,
                include_video_button=body.include_video_button,
                job_id=body.job_id,
            )
        except Exception as e:
            logger.error("[discord] send_file_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    logger.info("[discord] send_report done job_id=%s filename=%s", body.job_id, filename)
    return {
        "status": "sent",
        "filename": filename,
        "file_url": stored.presigned_url if stored else "",
        "s3_uri": report_storage_url,
    }


@app.post("/internal/send-text")
async def send_text(_: AuthDep, body: SendTextRequest) -> dict:
    """n8n WF-05에서 특정 채널로 텍스트 메시지를 전송한다."""
    try:
        await _discord_adapter.send_text_message(body.messenger_channel_id, body.text)
    except Exception as e:
        logger.error("[discord] send_text failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "sent"}


@app.post("/internal/send-audio")
async def send_audio(_: AuthDep, body: SendAudioRequest) -> dict:
    """WF-11에서 호출 — Discord로 TTS 완료본 전송(분기별 승인/반려 버튼 선택 노출)."""
    if not body.audio_content_b64:
        raise HTTPException(status_code=400, detail="audio_content_b64 is required")
    try:
        audio_bytes = base64.b64decode(body.audio_content_b64)
    except Exception as e:
        logger.error("[discord] audio base64 decode failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=400, detail=f"Invalid audio_content_b64: {e}")

    existing_job = await job_service.get_job(body.job_id)
    filename = _normalize_audio_filename(
        body.job_id,
        body.filename,
        existing_job.get("script_json") if existing_job else None,
    )
    stored = None
    try:
        # 오디오는 항상 S3 저장을 먼저 시도하고,
        # 실패 시에만 Discord attachment URL을 최종 URL로 사용한다.
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_tts,
            filename=filename,
            content=audio_bytes,
            content_type="audio/wav",
        )
    except Exception as e:
        logger.error("[storage] tts upload failed job_id=%s: %s", body.job_id, e)

    caption = body.caption or "🔊 TTS 완료본입니다. 일반 승인 또는 고화질 승인을 선택한 뒤 최종 확인을 진행하세요."
    if stored and stored.size_bytes > _discord_attachment_limit_bytes():
        # 큰 파일은 Discord 첨부 대신 presigned link + 버튼 메시지로 전송한다.
        try:
            message_id = await _discord_adapter.send_tts_link_message(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                caption=caption,
                audio_url=stored.presigned_url,
                include_wf12_button=body.include_wf12_button,
            )
            attachment_url = stored.presigned_url
        except Exception as e:
            logger.error("[discord] send_tts_link_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))
    else:
        # 기본 경로: Discord에 직접 WAV를 올리고 attachment URL을 받는다.
        if stored is None and len(audio_bytes) > _discord_attachment_limit_bytes():
            raise HTTPException(
                status_code=500,
                detail="TTS upload failed and audio exceeds Discord attachment size limit",
            )
        try:
            message_id, attachment_url = await _discord_adapter.send_tts_audio_message(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                caption=caption,
                audio_bytes=audio_bytes,
                filename=filename,
                include_wf12_button=body.include_wf12_button,
            )
        except Exception as e:
            logger.error("[discord] send_tts_audio_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    resolved_audio_url = stored.s3_uri if stored else attachment_url
    merged_script = _merge_script_json_with_media_names(
        existing_job.get("script_json") if existing_job else None,
        audio_filename=filename,
    )
    await job_service.update_job(
        body.job_id,
        confirm_message_id=message_id,
        audio_url=resolved_audio_url,
        final_url=stored.s3_uri if stored else attachment_url,
        script_json=merged_script,
    )
    logger.info("[discord] send_audio done job_id=%s filename=%s", body.job_id, filename)
    return {
        "status": "sent",
        "message_id": message_id,
        "attachment_url": attachment_url,
        "audio_url": stored.presigned_url if stored else attachment_url,
        "filename": filename,
        "s3_uri": stored.s3_uri if stored else "",
    }


@app.post("/internal/tts-action")
async def tts_action(_: AuthDep, body: TtsActionRequest) -> dict:
    """Discord TTS 완료본 승인/반려 버튼 처리."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]

    if body.action in {"approve_standard", "approve_hd"}:
        # WF-12는 외부 URL을 읽어야 하므로 s3:// 저장값은 presigned URL로 바꿔 넘긴다.
        resolved_avatar_id, avatar_source = await _resolve_heygen_avatar_id(job)
        audio_url = job.get("audio_url", "")
        if not audio_url:
            raise HTTPException(status_code=400, detail="No audio_url found in job")
        use_avatar_iv_model = body.action == "approve_hd" or body.use_avatar_iv_model
        approved_audio_url = audio_url
        if isinstance(audio_url, str) and audio_url.startswith("s3://"):
            try:
                approved_audio_url = presign_s3_uri(audio_url)
            except Exception as e:
                logger.error("[storage] presign audio s3 uri failed job_id=%s: %s", body.job_id, e)
                raise HTTPException(status_code=500, detail=f"audio presign failed: {e}")
        merged_script = _merge_script_json_with_media_names(
            job.get("script_json"),
            heygen_use_avatar_iv_model=use_avatar_iv_model,
        )
        await job_service.update_job(body.job_id, script_json=merged_script)
        try:
            await n8n_service.call_wf12_heygen_generate(
                job_id=body.job_id,
                channel_id=channel_id,
                user_id=user_id,
                audio_url=approved_audio_url,
                avatar_id=resolved_avatar_id,
                use_avatar_iv_model=use_avatar_iv_model,
            )
            mode_text = "고화질 Avatar IV" if use_avatar_iv_model else "일반"
            await _discord_adapter.send_text_message(
                channel_id,
                f"🎬 WF-12(HeyGen) 영상 생성을 시작합니다. 모드: {mode_text}",
            )
        except Exception as e:
            logger.error("call_wf12 (tts approve) failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))
        logger.info(
            "[discord] tts_action=%s trigger_wf12 job_id=%s avatar_source=%s use_avatar_iv_model=%s",
            body.action,
            body.job_id,
            avatar_source,
            use_avatar_iv_model,
        )
        return {
            "job_id": body.job_id,
            "action": body.action,
            "avatar_id": resolved_avatar_id,
            "avatar_source": avatar_source,
            "use_avatar_iv_model": use_avatar_iv_model,
        }

    if body.action == "reject":
        await job_service.transition_status(body.job_id, "APPROVED")
        await _discord_adapter.send_text_message(channel_id, "❌ TTS 반려됨. 필요 시 다시 TTS를 생성하세요.")
        return {"job_id": body.job_id, "action": "reject"}

    raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


@app.post("/internal/send-video-preview")
async def send_video_preview(_: AuthDep, body: SendVideoPreviewRequest) -> dict:
    """WF-12 완료 후 호출 — Discord로 영상 미리보기 + 승인/반려 버튼을 전송한다."""
    # WF-12가 준 video_url을 다시 내려받아 S3에 통일 저장한 뒤,
    # Discord에는 presigned preview 링크와 승인/반려 버튼을 보낸다.
    existing_job = await job_service.get_job(body.job_id)
    normalized_video_filename = _normalize_video_filename(
        body.job_id,
        body.video_filename,
        existing_job.get("script_json") if existing_job else None,
    )
    try:
        video_resp = await _http_client.get(body.video_url, timeout=300.0)
        video_resp.raise_for_status()
    except Exception as e:
        logger.error("[storage] video source download failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=f"Video download failed: {e}")

    try:
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_videos,
            filename=normalized_video_filename,
            content=video_resp.content,
            content_type=video_resp.headers.get("content-type") or "video/mp4",
        )
    except Exception as e:
        logger.error("[storage] video upload failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=f"Video upload failed: {e}")

    try:
        message_id = await _discord_adapter.send_video_preview(
            channel_id=body.channel_id,
            user_id=body.user_id,
            job_id=body.job_id,
            video_url=stored.presigned_url,
        )
    except Exception as e:
        logger.error("[discord] send_video_preview failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    existing_script_json = _as_script_json(existing_job.get("script_json") if existing_job else None)
    subtitle_script_text = _get_subtitle_script_text(existing_script_json)
    generated_content_id, generated_content_error = await _register_generated_content(
        job_id=body.job_id,
        script_text=subtitle_script_text,
        content_url=stored.s3_uri,
    )

    merged_script = _merge_script_json_with_media_names(
        existing_script_json,
        video_filename=normalized_video_filename,
        generated_content_id=generated_content_id if generated_content_id else None,
        generated_content_error=generated_content_error,
    )
    video_storage_url = stored.s3_uri
    await job_service.update_job(
        body.job_id,
        confirm_message_id=message_id,
        video_url=video_storage_url,
        final_url=video_storage_url,
        script_json=merged_script,
    )
    logger.info("[discord] send_video_preview done job_id=%s", body.job_id)
    return {
        "job_id": body.job_id,
        "message_id": message_id,
        "video_filename": normalized_video_filename,
        "video_url": stored.presigned_url,
        "s3_uri": video_storage_url,
    }


@app.post("/internal/video-action")
async def video_action(_: AuthDep, body: VideoActionRequest) -> dict:
    """Discord 영상 승인/반려 버튼 클릭을 처리한다."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]

    if body.action == "approved":
        # 최종 승인 시점에는 gateway가 직접 업로드하지 않고 WF-08에 SNS 업로드를 위임한다.
        video_url = job.get("video_url", "")
        if isinstance(video_url, str) and video_url.startswith("s3://"):
            try:
                video_url = presign_s3_uri(video_url)
            except Exception as e:
                logger.error("[storage] presign video s3 uri failed job_id=%s: %s", body.job_id, e)
                raise HTTPException(status_code=500, detail=f"video presign failed: {e}")
        script_json = _as_script_json(job.get("script_json"))
        media_names = script_json.get("media_names") if isinstance(script_json.get("media_names"), dict) else {}
        video_filename = _normalize_video_filename(body.job_id, media_names.get("video_filename"), script_json)

        try:
            await n8n_service.call_wf08_sns_upload(
                body.job_id,
                video_url,
                channel_id,
                video_filename=video_filename,
            )
        except Exception as e:
            logger.error("call_wf08_sns_upload failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=502, detail=f"WF-08 trigger failed: {e}")

        logger.info("[discord] video_action=approved job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "approved"}

    elif body.action == "reject_select":
        try:
            await _discord_adapter.send_reject_step_buttons(channel_id, body.job_id)
        except Exception as e:
            logger.error("[discord] send_reject_step_buttons failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))

        logger.info("[discord] video_action=reject_select job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "reject_select"}

    elif body.action == "reject_step":
        step = body.step or "draft"

        if step == "script":
            # script 단계로 되돌리면 승인 버튼이 달린 컨펌 메시지를 다시 생성한다.
            await job_service.transition_status(body.job_id, "WAITING_APPROVAL")
            confirm_message_id = job.get("confirm_message_id")
            script_json = _as_script_json(job.get("script_json"))
            title = script_json.get("title", "대본")
            subtitle_script_text = _get_subtitle_script_text(script_json)
            script_summary = script_json.get("script_summary") or subtitle_script_text[:100]
            try:
                new_msg_id = await _discord_adapter.send_confirm_message(
                    channel_id=channel_id,
                    user_id=user_id,
                    job_id=body.job_id,
                    title=title,
                    script_summary=script_summary,
                    preview_url=None,
                )
                await job_service.update_job(body.job_id, confirm_message_id=new_msg_id)
            except Exception as e:
                logger.error("[discord] re-send confirm failed job_id=%s: %s", body.job_id, e)

        elif step == "tts":
            # tts 단계로 되돌리면 현재 저장된 tts_script_text로 바로 재생성을 시작한다.
            await job_service.transition_status(body.job_id, "APPROVED")
            script_json = _as_script_json(job.get("script_json"))
            script_text = _get_tts_script_text(script_json)
            try:
                _spawn_tts_generation(
                    job_id=body.job_id,
                    script_text=script_text,
                    channel_id=channel_id,
                    user_id=user_id,
                    auto_trigger_wf12=False,
                )
                await _discord_adapter.send_text_message(channel_id, "🔊 TTS를 재생성합니다...")
            except Exception as e:
                logger.error("call_wf11 (tts retry) failed job_id=%s: %s", body.job_id, e)

        elif step == "draft":
            # draft는 기존 산출물을 버리고 사용자가 /create로 처음부터 다시 시작하게 만든다.
            await job_service.transition_status(body.job_id, "DRAFT")
            try:
                await _discord_adapter.send_text_message(channel_id, "🔄 처음부터 시작합니다. 새 콘셉트를 `/create`로 입력해주세요.")
            except Exception as e:
                logger.error("[discord] send_text_message failed job_id=%s: %s", body.job_id, e)

        logger.info("[discord] video_action=reject_step step=%s job_id=%s", step, body.job_id)
        return {"job_id": body.job_id, "action": "reject_step", "step": step}

    raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


@app.post("/internal/report-to-tts")
async def report_to_tts(_: AuthDep, body: ReportToTtsRequest) -> dict:
    """/report 결과의 'TTS만 제작' 버튼 클릭 처리 — WF-11(TTS)만 트리거."""
    # 보고서가 이미 있으므로 WF-06을 다시 돌리지 않고 TTS 생성만 따로 시작한다.
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    script_json = _as_script_json(job.get("script_json"))
    script_text = _get_tts_script_text(script_json)
    if not script_text:
        raise HTTPException(status_code=400, detail="No script found in job")

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]

    await job_service.transition_status(body.job_id, "APPROVED")

    try:
        _spawn_tts_generation(
            job_id=body.job_id,
            script_text=script_text,
            channel_id=channel_id,
            user_id=user_id,
            auto_trigger_wf12=False,
        )
        await _discord_adapter.send_text_message(
            channel_id,
            "🔊 TTS 생성을 시작합니다. 완료본 확인 후 승인하면 WF-12(HeyGen)로 진행됩니다.",
        )
    except Exception as e:
        logger.error("call_wf11 (report_to_tts) failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("[discord] report_to_tts triggered job_id=%s", body.job_id)
    return {"job_id": body.job_id, "status": "triggered", "mode": "tts-only"}


@app.post("/internal/report-to-video")
async def report_to_video(_: AuthDep, body: ReportToVideoRequest) -> dict:
    """/report 결과의 '영상으로 제작' 버튼 클릭 처리 — TTS 생성 후 Discord 승인 단계로 넘긴다."""
    # 보고서 -> TTS까지만 자동 진행하고,
    # WF-12는 TTS 완료 후 Discord 버튼에서 일반/고화질 중 선택한다.
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if (body.avatar_id or "").strip():
        job = await _persist_job_avatar_override(body.job_id, job.get("script_json"), body.avatar_id)
    resolved_avatar_id, avatar_source = await _resolve_heygen_avatar_id(job, requested_avatar_id=body.avatar_id)

    script_json = _as_script_json(job.get("script_json"))
    script_text = _get_tts_script_text(script_json)
    if not script_text:
        raise HTTPException(status_code=400, detail="No script found in job")

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]

    await job_service.transition_status(body.job_id, "APPROVED")

    try:
        _spawn_tts_generation(
            job_id=body.job_id,
            script_text=script_text,
            channel_id=channel_id,
            user_id=user_id,
            auto_trigger_wf12=False,
        )
        await _discord_adapter.send_text_message(
            channel_id,
            "🎬 영상 제작 준비를 시작합니다. TTS 완료 후 일반 승인 또는 고화질 승인을 선택하고, 최종 확인 후 영상을 생성하세요.",
        )
    except Exception as e:
        logger.error("call_wf11 (report_to_video) failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("[discord] report_to_video triggered job_id=%s avatar_source=%s", body.job_id, avatar_source)
    return {
        "job_id": body.job_id,
        "status": "triggered",
        "mode": "video-prepare",
        "avatar_id": resolved_avatar_id,
        "avatar_source": avatar_source,
    }


@app.post("/internal/tts-generate")
async def tts_generate(_: AuthDep, body: ManualGenerateRequest) -> dict:
    """수동 /tts 명령 처리 — job_id(전체/접두) 또는 최근 작업으로 WF-11 실행."""
    normalized_prompt = (body.prompt or "").strip()
    logger.info(
        "[manual /tts] request job_id=%s user=%s channel=%s prompt_len=%d",
        (body.job_id or "").strip(),
        (body.messenger_user_id or "").strip(),
        (body.messenger_channel_id or "").strip(),
        len(normalized_prompt),
    )
    if normalized_prompt and (body.job_id or "").strip():
        raise HTTPException(status_code=400, detail="job_id and prompt cannot be used together")

    if normalized_prompt:
        job = await _create_prompt_tts_job(body)
        resolved_job_id = job["id"]
    else:
        job = await _resolve_manual_job(body, require_script=True)
        resolved_job_id = job["id"]

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]
    script_json = _as_script_json(job.get("script_json"))
    script_text = _get_tts_script_text(script_json)
    if not script_text:
        raise HTTPException(status_code=400, detail="No script_text found in resolved job")

    await job_service.transition_status(resolved_job_id, "APPROVED")
    try:
        _spawn_tts_generation(
            job_id=resolved_job_id,
            script_text=script_text,
            channel_id=channel_id,
            user_id=user_id,
            auto_trigger_wf12=False,
        )
    except Exception as e:
        logger.error("call_wf11 (manual /tts) failed job_id=%s: %s", resolved_job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    logger.info(
        "[manual /tts] triggered job_id=%s source=%s",
        resolved_job_id,
        "prompt" if normalized_prompt else "existing-job",
    )
    return {
        "job_id": resolved_job_id,
        "status": "triggered",
        "workflow": "WF-11",
        "source": "prompt" if normalized_prompt else "existing-job",
    }


@app.post("/internal/heygen-smoke-test")
async def heygen_smoke_test(_: AuthDep, body: HeygenSmokeTestRequest) -> dict:
    """HeyGen 과금 없는 스모크 테스트 — 인증과 avatar 접근만 검증한다."""
    configured_avatar_id = (body.avatar_id or settings.heygen_avatar_id).strip()
    avatars_payload = await _heygen_get_json("/v2/avatars")

    avatars_data = avatars_payload.get("data") if isinstance(avatars_payload.get("data"), dict) else {}
    avatars = avatars_data.get("avatars") if isinstance(avatars_data.get("avatars"), list) else []

    avatar_check: dict[str, object] = {
        "avatar_id": configured_avatar_id,
        "configured": bool(configured_avatar_id),
        "exists": False,
        "name": "",
    }
    if configured_avatar_id:
        try:
            avatar_payload = await _heygen_get_json(f"/v2/avatar/{configured_avatar_id}/details")
            avatar_data = avatar_payload.get("data") if isinstance(avatar_payload.get("data"), dict) else {}
            avatar = avatar_data.get("avatar") if isinstance(avatar_data.get("avatar"), dict) else avatar_data
            if isinstance(avatar, dict):
                avatar_check["exists"] = True
                avatar_check["name"] = str(
                    avatar.get("avatar_name")
                    or avatar.get("name")
                    or ""
                ).strip()
        except HTTPException as e:
            avatar_check["error"] = e.detail

    logger.info(
        "[heygen-smoke] avatars=%d configured_avatar=%s exists=%s",
        len(avatars),
        configured_avatar_id,
        avatar_check.get("exists"),
    )
    return {
        "status": "ok",
        "avatars_count": len(avatars),
        "configured_avatar": avatar_check,
        "video_defaults": {
            "width": settings.heygen_video_width,
            "height": settings.heygen_video_height,
            "caption_enabled": settings.heygen_caption_enabled,
            "speed": settings.heygen_speed,
            "poll_interval_seconds": settings.heygen_poll_interval_seconds,
            "max_wait_seconds": settings.heygen_max_wait_seconds,
            "mock_enabled": settings.heygen_mock_enabled,
            "mock_video_url": settings.heygen_mock_video_url,
        },
    }


@app.post("/internal/character-avatar")
async def set_character_avatar(_: AuthDep, body: CharacterAvatarRequest) -> dict:
    """캐릭터 기본 HeyGen avatar_id를 DB 상태로 저장한다."""
    updated = await job_service.update_character_avatar(body.character_id.strip(), body.avatar_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Character not found")
    return {
        "character_id": updated["id"],
        "heygen_avatar_id": str(updated.get("heygen_avatar_id") or ""),
    }


@app.post("/internal/heygen-generate")
async def heygen_generate(_: AuthDep, body: ManualGenerateRequest) -> dict:
    raise HTTPException(
        status_code=410,
        detail="Manual /heygen is disabled. Use the TTS approval buttons to choose standard or high-quality WF-12.",
    )


@app.post("/internal/jobs")
async def list_jobs(_: AuthDep, body: ListJobsRequest) -> dict:
    """Discord 수동 명령 보조용 최근 job 목록을 반환한다."""
    purpose = (body.purpose or "all").strip().lower()
    if purpose not in ("all", "tts", "heygen"):
        raise HTTPException(status_code=400, detail="purpose must be one of: all, tts, heygen")

    require_script = purpose == "tts"
    require_audio = purpose == "heygen"
    rows = await job_service.list_recent_jobs(
        body.messenger_user_id,
        body.messenger_channel_id,
        limit=body.limit,
        require_script=require_script,
        require_audio=require_audio,
    )

    jobs = [
        {
            "job_id": row["id"],
            "job_id_short": row["id"][:8],
            "status": row.get("status", ""),
            "created_at": _to_iso8601(row.get("created_at")),
            "updated_at": _to_iso8601(row.get("updated_at")),
            "has_script_text": bool((row.get("script_text") or "").strip()),
            "has_audio_url": bool((row.get("audio_url") or "").strip()),
        }
        for row in rows
    ]

    return {"purpose": purpose, "count": len(jobs), "jobs": jobs}


@app.get("/health")
async def health() -> dict:
    try:
        pool = await job_service.get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "connected"
    except Exception as e:
        logger.error("health check DB failed: %s", e)
        db_status = "error"

    return {
        "status": "ok",
        "db": db_status,
        "adapters": ["discord"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.gateway_port)
