import asyncio
import logging
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
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
    CostEventIngestRequest,
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
    SeedLabProgressRequest,
    SeedLabRefreshLinkRequest,
    SeedLabStartRequest,
    TtsActionRequest,
    VideoActionRequest,
    Wf13RunBatchRequest,
    Wf13PreflightRequest,
    Wf13RunJobRequest,
)
from services import cost_service, job_service, n8n_service

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
_TTS_VARIANT_COUNT = 3
_WF13_TTS_MAX_ATTEMPTS = 3
_WF13_TTS_RETRY_BACKOFF_SECONDS = (20, 60)
_WF13_PUBLISH_MAX_ATTEMPTS = 3
_WF13_PUBLISH_RETRY_BACKOFF_SECONDS = (10, 30)
_HEYGEN_AVATAR_OPTION_COUNT = 6
_HEYGEN_AVATAR_LABEL_PREFIX_LEN = 6
_http_basic = HTTPBasic()
_HARDBURN_FONT_NAME = "Noto Sans CJK KR"
_HARDBURN_FONT_DIRS = (
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/truetype/custom",
    "/usr/share/fonts",
)
_HARDBURN_FORCE_STYLE = (
    "FontName=Jua,"
    "FontSize=13,"
    "PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,"
    "BackColour=&H00000000,"
    "BorderStyle=1,"
    "Outline=0,"
    "Shadow=0,"
    "MarginV=30,"
    "Alignment=2"
)


class Wf13PipelineError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "",
        failure_stage: str = "",
        retryable: bool = False,
        should_stop_workflow: bool = False,
        blocked_reason: str = "",
        user_notified: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = str(error_type or "").strip()
        self.failure_stage = str(failure_stage or "").strip()
        self.retryable = bool(retryable)
        self.should_stop_workflow = bool(should_stop_workflow)
        self.blocked_reason = str(blocked_reason or "").strip()
        self.user_notified = bool(user_notified)


def _ffmpeg_filter_escape(value: str) -> str:
    escaped = str(value or "")
    escaped = escaped.replace("\\", r"\\")
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    escaped = escaped.replace("[", r"\[")
    escaped = escaped.replace("]", r"\]")
    escaped = escaped.replace(",", r"\,")
    return escaped


def _resolve_hardburn_font_dir() -> str:
    for candidate in _HARDBURN_FONT_DIRS:
        if not os.path.isdir(candidate):
            continue
        try:
            entries = os.listdir(candidate)
        except OSError:
            continue
        if any(entry.lower().endswith((".ttf", ".otf", ".ttc")) for entry in entries):
            return candidate
    raise RuntimeError(f"hardburn font directory not found: {_HARDBURN_FONT_DIRS}")


def _estimate_script_rewrite_cost_usd(usage: dict[str, Any]) -> Optional[float]:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return None
    input_rate = float(settings.script_rewrite_input_cost_usd_per_1m)
    output_rate = float(settings.script_rewrite_output_cost_usd_per_1m)
    return ((prompt_tokens / 1_000_000.0) * input_rate) + ((completion_tokens / 1_000_000.0) * output_rate)


def _estimated_pricing_kind(cost_usd: Optional[float]) -> str:
    return "estimated" if cost_usd is not None else "missing"


def _estimated_pricing_source(cost_usd: Optional[float]) -> str:
    return "provider_usage_estimate" if cost_usd is not None else "unavailable"


def _estimate_tts_cost_usd(script_text: str) -> Optional[float]:
    per_1k = float(settings.tts_cost_usd_per_1k_chars)
    if per_1k <= 0:
        return None
    char_count = max(0, _script_char_count(script_text))
    return (char_count / 1000.0) * per_1k


def _estimate_tts_cost_usd_from_duration(duration_ms: float) -> Optional[float]:
    """RunPod Pod GPU 시간 기반 TTS 비용 계산 (RUNPOD_GPU_COST_USD_PER_HOUR 설정 시 우선 사용)."""
    gpu_rate = float(settings.runpod_gpu_cost_usd_per_hour)
    if gpu_rate <= 0 or duration_ms <= 0:
        return None
    return (duration_ms / 1000.0) / 3600.0 * gpu_rate


def _extract_heygen_cost_candidate(payload: Any) -> Optional[float]:
    if isinstance(payload, (int, float)):
        try:
            return float(payload)
        except Exception:
            return None
    if isinstance(payload, dict):
        for key in (
            "cost_usd",
            "costUsd",
            "credits_cost_usd",
            "creditsCostUsd",
            "total_cost_usd",
            "totalCostUsd",
            "billed_cost_usd",
            "billedCostUsd",
        ):
            if key in payload:
                candidate = _extract_heygen_cost_candidate(payload.get(key))
                if candidate is not None:
                    return candidate
        # NOTE: "cost" 키는 HeyGen Credits 단위로 USD가 아님 → 의도적으로 제외
        for key in ("billing", "usage", "data", "response_snapshot", "poll_response", "create_response"):
            if key in payload:
                candidate = _extract_heygen_cost_candidate(payload.get(key))
                if candidate is not None:
                    return candidate
    if isinstance(payload, list):
        for item in payload:
            candidate = _extract_heygen_cost_candidate(item)
            if candidate is not None:
                return candidate
    return None


def _resolve_heygen_cost(raw_cost: Optional[float], *payloads: Any) -> tuple[Optional[float], str, str]:
    if raw_cost is not None:
        try:
            return float(raw_cost), "actual", "provider_actual"
        except Exception:
            pass
    for payload in payloads:
        candidate = _extract_heygen_cost_candidate(payload)
        if candidate is not None:
            return candidate, "actual", "provider_actual"
    fallback = float(settings.heygen_fallback_cost_usd_per_video)
    if fallback > 0:
        return fallback, "estimated", "config_fallback"
    return None, "missing", "unavailable"


async def _record_cost_event_safe(**kwargs: Any) -> None:
    try:
        await cost_service.record_event(**kwargs)
    except Exception as e:
        logger.warning("[cost] record event failed kwargs_keys=%s err=%s", list(kwargs.keys()), e)


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


def _build_tts_variant_filename(job_id: str, variant_index: int) -> str:
    base_filename = build_filename(job_id, "wav")
    stem, ext = base_filename.rsplit(".", 1)
    return f"{stem}-v{variant_index + 1}.{ext}"


def _normalize_video_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "mp4", candidate, existing_script_json)


def _build_hardburn_video_filename(raw_video_filename: str) -> str:
    stem, _ = raw_video_filename.rsplit(".", 1)
    return f"{stem}.hardburn.mp4"


def _build_srt_filename(raw_video_filename: str) -> str:
    stem, _ = raw_video_filename.rsplit(".", 1)
    return f"{stem}.srt"


def _merge_script_json_with_hardburn_failure(
    existing_script_json: dict[str, Any] | None,
    *,
    raw_video_filename: str,
    hardburn_video_filename: str,
    srt_filename: str,
    raw_video_s3_uri: str,
    srt_s3_uri: str,
    error: str,
) -> dict[str, Any]:
    merged_script = _as_script_json(existing_script_json)
    media_names = merged_script.get("media_names") if isinstance(merged_script.get("media_names"), dict) else {}
    media_names["raw_video_filename"] = raw_video_filename
    media_names["hardburn_video_filename"] = hardburn_video_filename
    media_names["srt_filename"] = srt_filename
    merged_script["media_names"] = media_names
    merged_script["video_hardburn"] = {
        "status": "failed",
        "error": str(error or ""),
        "raw_video_s3_uri": raw_video_s3_uri,
        "hardburn_video_s3_uri": "",
        "srt_s3_uri": srt_s3_uri,
    }
    return merged_script


def _merge_script_json_with_video_preview_failure(
    existing_script_json: dict[str, Any] | None,
    *,
    raw_video_filename: str = "",
    hardburn_video_filename: str = "",
    srt_filename: str = "",
    raw_video_s3_uri: str = "",
    hardburn_video_s3_uri: str = "",
    srt_s3_uri: str = "",
    failed_stage: str = "",
    error: str,
    caption_artifacts: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    merged_script = _as_script_json(existing_script_json)
    media_names = merged_script.get("media_names") if isinstance(merged_script.get("media_names"), dict) else {}
    if raw_video_filename:
        media_names["raw_video_filename"] = raw_video_filename
    if hardburn_video_filename:
        media_names["hardburn_video_filename"] = hardburn_video_filename
    if srt_filename:
        media_names["srt_filename"] = srt_filename
    if media_names:
        merged_script["media_names"] = media_names

    video_assets = merged_script.get("video_assets") if isinstance(merged_script.get("video_assets"), dict) else {}
    if raw_video_s3_uri:
        video_assets["raw_video_s3_uri"] = raw_video_s3_uri
    if hardburn_video_s3_uri:
        video_assets["hardburn_video_s3_uri"] = hardburn_video_s3_uri
    if srt_s3_uri:
        video_assets["srt_s3_uri"] = srt_s3_uri
    if video_assets:
        merged_script["video_assets"] = video_assets

    video_hardburn = merged_script.get("video_hardburn") if isinstance(merged_script.get("video_hardburn"), dict) else {}
    video_hardburn.update(
        {
            "status": "failed",
            "error": str(error or ""),
            "failed_stage": str(failed_stage or ""),
            "raw_video_s3_uri": raw_video_s3_uri or str(video_hardburn.get("raw_video_s3_uri") or ""),
            "hardburn_video_s3_uri": hardburn_video_s3_uri or str(video_hardburn.get("hardburn_video_s3_uri") or ""),
            "srt_s3_uri": srt_s3_uri or str(video_hardburn.get("srt_s3_uri") or ""),
        }
    )
    if isinstance(caption_artifacts, dict):
        if caption_artifacts.get("subtitle_sha256"):
            video_hardburn["subtitle_sha256"] = caption_artifacts.get("subtitle_sha256")
        cue_count = caption_artifacts.get("cue_count") or caption_artifacts.get("display_sentence_count")
        if cue_count:
            video_hardburn["cue_count"] = cue_count
        if caption_artifacts.get("asr_model"):
            video_hardburn["asr_model"] = caption_artifacts.get("asr_model")
        if caption_artifacts.get("matched_sentence_count") is not None:
            video_hardburn["matched_sentence_count"] = caption_artifacts.get("matched_sentence_count")
        if caption_artifacts.get("word_count") is not None:
            video_hardburn["word_count"] = caption_artifacts.get("word_count")
        if caption_artifacts.get("alignment_status"):
            video_hardburn["alignment_status"] = caption_artifacts.get("alignment_status")
        if caption_artifacts.get("timing_source"):
            video_hardburn["timing_source"] = caption_artifacts.get("timing_source")
        if caption_artifacts.get("fallback_reason"):
            video_hardburn["fallback_reason"] = caption_artifacts.get("fallback_reason")
        if isinstance(caption_artifacts.get("request_json"), dict):
            video_hardburn["request_json"] = caption_artifacts.get("request_json")
    merged_script["video_hardburn"] = video_hardburn
    return merged_script


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


async def _heygen_post_json(path: str, *, payload: dict[str, Any], base_url: Optional[str] = None) -> dict:
    if _http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client is not initialized")
    target_base = (base_url or settings.heygen_api_base_url).rstrip("/")
    url = f"{target_base}/{path.lstrip('/')}"
    try:
        resp = await _http_client.post(
            url,
            headers={**_heygen_api_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=max(_HEYGEN_REQUEST_TIMEOUT_SECONDS, 300.0),
        )
        resp.raise_for_status()
        response_payload = resp.json()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        detail = e.response.text.strip() or str(e)
        raise HTTPException(status_code=502, detail=f"HeyGen API request failed: {detail}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HeyGen API request failed: {e}") from e
    if not isinstance(response_payload, dict):
        raise HTTPException(status_code=502, detail="HeyGen API returned a non-JSON response")
    return response_payload


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


def _next_tts_batch_id() -> str:
    return uuid.uuid4().hex[:12]


def _build_random_tts_variant_seeds() -> list[int]:
    seeds: list[int] = []
    while len(seeds) < _TTS_VARIANT_COUNT:
        seed = (uuid.uuid4().int % 2_147_483_647) or len(seeds) + 1
        if seed not in seeds:
            seeds.append(seed)
    return seeds


def _parse_tts_seed_list(raw_value: object, *, source_name: str) -> list[int]:
    if raw_value is None:
        return []

    if isinstance(raw_value, str):
        tokens = [token.strip() for token in raw_value.split(",") if token.strip()]
    elif isinstance(raw_value, (list, tuple)):
        tokens = list(raw_value)
    else:
        raise ValueError(f"{source_name}: unsupported type={type(raw_value).__name__}")

    if len(tokens) != _TTS_VARIANT_COUNT:
        raise ValueError(f"{source_name}: expected {_TTS_VARIANT_COUNT} seeds, got {len(tokens)}")

    seeds: list[int] = []
    for idx, token in enumerate(tokens):
        try:
            seed = int(str(token).strip())
        except Exception as e:
            raise ValueError(f"{source_name}: invalid int at index={idx}") from e
        if seed <= 0 or seed > 2_147_483_647:
            raise ValueError(f"{source_name}: out of range seed={seed} index={idx}")
        if seed in seeds:
            raise ValueError(f"{source_name}: duplicate seed={seed}")
        seeds.append(seed)
    return seeds


def _resolve_tts_variant_seeds(channel_id: str, *, force_random: bool = False) -> tuple[list[int], str]:
    if force_random:
        return _build_random_tts_variant_seeds(), "random_regenerate"

    had_invalid_config = False

    default_raw = (settings.tts_fixed_seeds or "").strip()
    if default_raw:
        try:
            return _parse_tts_seed_list(default_raw, source_name="TTS_FIXED_SEEDS"), "fixed_default"
        except Exception as e:
            had_invalid_config = True
            logger.warning("[tts-seed] invalid TTS_FIXED_SEEDS config err=%s", e)

    return _build_random_tts_variant_seeds(), ("random_fallback" if had_invalid_config else "random")


def _get_tts_variants(script_json: dict) -> list[dict]:
    variants = script_json.get("tts_variants")
    if not isinstance(variants, list):
        return []
    result: list[dict] = []
    for item in variants:
        if isinstance(item, dict):
            result.append(dict(item))
    return result


def _clear_tts_variant_metadata(script_json: dict) -> dict:
    merged = _as_script_json(script_json)
    for key in (
        "tts_variants",
        "active_tts_batch_id",
        "selected_tts_variant_index",
        "selected_tts_seed",
        "selected_tts_timing",
        "tts_variant_control_message_id",
        "tts_variant_action_message_id",
        "tts_downstream_intent",
        "tts_seed_strategy",
        "tts_seed_values",
        "tts_variant_failures",
        "tts_failure_summary",
        "tts_last_error_at",
        "heygen_avatar_id",
        "avatar_id",
        "heygen_avatar_label",
        "heygen_avatar_index",
    ):
        merged.pop(key, None)
    media_names = merged.get("media_names")
    if isinstance(media_names, dict):
        media_names.pop("audio_filename", None)
        if media_names:
            merged["media_names"] = media_names
        else:
            merged.pop("media_names", None)
    return merged


def _get_tts_downstream_intent(script_json: dict) -> str:
    intent = str(script_json.get("tts_downstream_intent") or "").strip()
    return intent or "tts_only"


def _get_wf13_state(script_json: dict) -> dict[str, Any]:
    raw = script_json.get("wf13_auto")
    return dict(raw) if isinstance(raw, dict) else {}


def _merge_script_json_with_wf13_state(
    existing: object,
    *,
    state: str,
    seed: int,
    avatar_id: str,
    use_avatar_iv_model: bool,
    targets: list[str],
    last_completed_stage: str = "",
    last_error: str = "",
    retryable: Optional[bool] = None,
    blocked_reason: str = "",
    tts_attempts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    merged = _as_script_json(existing)
    wf13_state = _get_wf13_state(merged)
    wf13_state["state"] = state
    wf13_state["seed"] = int(seed)
    wf13_state["avatar_id"] = str(avatar_id or "").strip()
    wf13_state["use_avatar_iv_model"] = bool(use_avatar_iv_model)
    wf13_state["targets"] = [str(target).strip().lower() for target in targets if str(target).strip()]
    if not wf13_state.get("started_at"):
        wf13_state["started_at"] = datetime.now(timezone.utc).isoformat()
    if last_completed_stage:
        wf13_state["last_completed_stage"] = last_completed_stage
    if last_error:
        wf13_state["last_error"] = str(last_error)
        wf13_state.pop("finished_at", None)
    else:
        wf13_state["last_error"] = ""
        if state in {"completed", "skipped"}:
            wf13_state["finished_at"] = datetime.now(timezone.utc).isoformat()
    if retryable is not None:
        wf13_state["retryable"] = bool(retryable)
    if blocked_reason:
        wf13_state["blocked_reason"] = str(blocked_reason)
    elif state != "blocked":
        wf13_state.pop("blocked_reason", None)
    if tts_attempts is not None:
        wf13_state["tts_attempts"] = list(tts_attempts)
    merged["wf13_auto"] = wf13_state
    return merged


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


def _strip_fixed_intro_outro_lines(script_text: str) -> tuple[str, bool, bool]:
    raw_lines = [line.strip() for line in (script_text or "").splitlines() if line.strip()]
    removed_opening = False
    removed_ending = False
    if raw_lines and raw_lines[0] == TTS_SCRIPT_OPENING_LINE:
        raw_lines = raw_lines[1:]
        removed_opening = True
    if raw_lines and raw_lines[-1] == SCRIPT_ENDING_LINE:
        raw_lines = raw_lines[:-1]
        removed_ending = True
    return "\n".join(raw_lines).strip(), removed_opening, removed_ending


def _get_job_avatar_override(script_json: dict) -> str:
    return (
        script_json.get("heygen_avatar_id")
        or script_json.get("avatar_id")
        or ""
    ).strip()


def _get_job_avatar_index(script_json: dict) -> Optional[int]:
    raw = script_json.get("heygen_avatar_index")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def _get_job_avatar_label(script_json: dict) -> str:
    return str(script_json.get("heygen_avatar_label") or "").strip()


def _parse_heygen_avatar_options_from_env() -> list[dict[str, object]]:
    raw = (settings.heygen_avatar_id or "").strip()
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    expected = _HEYGEN_AVATAR_OPTION_COUNT
    if len(tokens) != expected:
        raise HTTPException(
            status_code=400,
            detail=f"HEYGEN_AVATAR_ID must contain exactly {expected} comma-separated avatar IDs.",
        )
    if len(set(tokens)) != expected:
        raise HTTPException(
            status_code=400,
            detail="HEYGEN_AVATAR_ID must not contain duplicate avatar IDs.",
        )
    options: list[dict[str, object]] = []
    for idx, avatar_id in enumerate(tokens):
        label = avatar_id[:_HEYGEN_AVATAR_LABEL_PREFIX_LEN] if avatar_id else f"#{idx}"
        options.append(
            {
                "index": idx,
                "label": label,
                "avatar_id": avatar_id,
            }
        )
    return options


def _resolve_selected_heygen_avatar(script_json: dict) -> tuple[str, str, int]:
    options = _parse_heygen_avatar_options_from_env()
    selected_index = _get_job_avatar_index(script_json)
    if selected_index is None:
        allowed_labels = "/".join(str(option["label"]) for option in options)
        raise HTTPException(
            status_code=400,
            detail=f"아바타를 먼저 선택하세요. {allowed_labels} 버튼 중 하나를 눌러주세요.",
        )
    if selected_index < 0 or selected_index >= len(options):
        raise HTTPException(
            status_code=400,
            detail="선택된 아바타가 유효하지 않습니다. 아바타를 다시 선택해주세요.",
        )
    selected = options[selected_index]
    return str(selected["avatar_id"]), str(selected["label"]), int(selected_index)


def _normalize_publish_targets(raw_targets: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw_target in raw_targets:
        target = str(raw_target or "").strip().lower()
        if not target or target in normalized:
            continue
        if target not in {"youtube", "instagram"}:
            raise HTTPException(status_code=400, detail=f"unsupported publish target: {target}")
        normalized.append(target)
    if not normalized:
        raise HTTPException(status_code=400, detail="at least one publish target is required")
    return normalized


def _normalized_platform_status(value: object) -> str:
    return str(value or "").strip().lower()


def _coerce_utc_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _publish_age_seconds(updated_at: object) -> Optional[float]:
    updated = _coerce_utc_datetime(updated_at)
    if updated is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())


def _build_publish_title(job_id: str, video_filename: str, subtitle_script_text: str, concept_text: str) -> str:
    lines = [line.strip() for line in subtitle_script_text.splitlines() if line.strip()]
    title_candidate = ""
    for idx, line in enumerate(lines):
        if line.startswith(SUBTITLE_SCRIPT_OPENING_LINE):
            if idx + 1 < len(lines):
                title_candidate = lines[idx + 1]
            continue
        title_candidate = line
        break
    if not title_candidate:
        title_candidate = (concept_text or "").strip()
    if not title_candidate:
        title_candidate = re.sub(r"\.mp4$", "", video_filename or "", flags=re.IGNORECASE).strip()
    if not title_candidate:
        title_candidate = f"Hari {job_id[:8]}"
    normalized = re.sub(r"\s+", " ", title_candidate).strip()
    return normalized[:95].rstrip()


def _build_publish_description(subtitle_script_text: str) -> str:
    return subtitle_script_text.strip()[:4500]


def _build_publish_caption(subtitle_script_text: str) -> str:
    return subtitle_script_text.strip()[:2200]


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


def _resolve_internal_media_url(media_url: str) -> str:
    cleaned = str(media_url or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("s3://"):
        return presign_s3_uri(cleaned)
    return cleaned


async def _build_caption_artifacts(
    *,
    job_id: str,
    audio_url: str,
    subtitle_script_text: str,
    tts_script_text: str,
    selected_tts_timing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    service_base_url = settings.sns_publisher_service_url.rstrip("/")
    if not service_base_url:
        raise RuntimeError("SNS_PUBLISHER_SERVICE_URL is not configured")
    resolved_audio_url = _resolve_internal_media_url(audio_url)
    if not resolved_audio_url:
        raise RuntimeError("audio_url is required for caption artifacts")
    resp = await _http_client.post(
        f"{service_base_url}/internal/caption-artifacts",
        json={
            "job_id": job_id,
            "audio_url": resolved_audio_url,
            "subtitle_script_text": subtitle_script_text,
            "tts_script_text": tts_script_text,
            "selected_tts_timing": dict(selected_tts_timing or {}),
        },
        timeout=300.0,
    )
    resp.raise_for_status()
    payload = resp.json() if resp.content else {}
    if str(payload.get("status") or "").strip() != "ready":
        raise RuntimeError(f"caption artifacts not ready: {payload}")
    srt_content = str(payload.get("srt_content") or "")
    if not srt_content.strip():
        raise RuntimeError("caption artifacts returned empty srt_content")
    return payload


async def _wf13_notify(channel_id: str, text: str) -> None:
    try:
        await _discord_adapter.send_text_message(channel_id, text)
    except Exception as e:
        logger.warning("[wf13] notify failed channel=%s err=%s", channel_id, e)


def _wf13_distinct_channel_ids(raw_channel_ids: list[str]) -> list[str]:
    channel_ids: list[str] = []
    for raw in raw_channel_ids or []:
        value = str(raw or "").strip()
        if value and value not in channel_ids:
            channel_ids.append(value)
    return channel_ids


def _wf13_extract_heygen_api_quota(payload: dict[str, Any]) -> Optional[float]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    for candidate in (details.get("api"), data.get("remaining_quota")):
        try:
            if candidate is None or candidate == "":
                continue
            return float(candidate)
        except Exception:
            continue
    return None


def _wf13_is_heygen_quota_error(detail: str) -> bool:
    normalized = (detail or "").lower()
    return "movio_payment_insufficient_credit" in normalized or "insufficient credit" in normalized


def _wf13_should_retry_tts_error(detail: str) -> bool:
    error_type = _extract_tts_error_type(detail)
    status_code = _extract_http_status_code_from_tts_error(detail)
    return error_type == "tts_network_error" or status_code in {408, 429, 502, 503, 504, 524}


def _wf13_extract_publish_error(result_payload: dict[str, Any]) -> str:
    if not isinstance(result_payload, dict):
        return ""
    results = result_payload.get("results") if isinstance(result_payload.get("results"), dict) else {}
    youtube = results.get("youtube") if isinstance(results.get("youtube"), dict) else {}
    for candidate in (youtube.get("error_message"), result_payload.get("summary_text")):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _wf13_is_upload_limit_error(detail: str) -> bool:
    normalized = (detail or "").lower()
    return (
        "uploadlimitexceeded" in normalized
        or "the user has exceeded the number of videos they may upload" in normalized
    )


def _wf13_is_transient_publish_error(detail: str, *, status_code: Optional[int] = None) -> bool:
    normalized = (detail or "").lower()
    transient_markers = (
        "backenderror",
        "internalerror",
        "ratelimitexceeded",
        "too many requests",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "connection lost",
        "temporarily unavailable",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    )
    if status_code in {408, 429, 500, 502, 503, 504}:
        return True
    return any(marker in normalized for marker in transient_markers)


async def _wf13_notify_publish_limit(channel_id: str, job_id: str, video_url: str) -> None:
    await _wf13_notify(
        channel_id,
        "\n".join(
            [
                "🎬 WF-13 자동 영상 생성은 완료되었습니다.",
                "",
                video_url,
                "",
                "⚠️ YouTube 자동업로드는 한도 초과로 실패했습니다.",
                f"Job ID: {job_id[:8]}",
            ]
        ),
    )


async def _wf13_check_heygen_quota(*, channel_ids: list[str], notify: bool) -> dict[str, Any]:
    payload = await _heygen_get_json("/v2/user/remaining_quota")
    api_quota = _wf13_extract_heygen_api_quota(payload)
    ok = api_quota is None or api_quota > 0
    notified_channel_ids: list[str] = []
    blocked_reason = ""
    if not ok:
        blocked_reason = "heygen_insufficient_credit"
        if notify:
            message = "⚠️ WF-13 자동 영상 생성이 중단되었습니다. HeyGen API credit이 부족합니다. 충전 후 다시 실행해야 합니다."
            for channel_id in _wf13_distinct_channel_ids(channel_ids):
                await _wf13_notify(channel_id, message)
                notified_channel_ids.append(channel_id)
    return {
        "ok": ok,
        "blocked_reason": blocked_reason,
        "remaining_quota": api_quota,
        "api_quota": api_quota,
        "notified_channel_ids": notified_channel_ids,
        "raw": payload,
    }


async def _list_wf13_candidate_jobs() -> list[dict[str, Any]]:
    pool = await job_service.get_db_pool()
    rows = await pool.fetch(
        """
        SELECT
            j.id::text AS job_id,
            COALESCE(j.messenger_channel_id, '') AS channel_id,
            COALESCE(j.status, '') AS status,
            COALESCE(j.script_json->'wf13_auto'->>'state', '') AS wf13_state,
            j.created_at
        FROM jobs j
        WHERE j.messenger_user_id = 'system:auto-report'
          AND COALESCE(j.status, '') IN ('REPORT_READY', 'PUBLISH_FAILED')
          AND COALESCE(j.script_json->>'subtitle_script_text', '') <> ''
          AND COALESCE(j.script_json->>'tts_script_text', '') <> ''
          AND j.created_at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul')
          AND j.created_at < ((date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') + interval '1 day') AT TIME ZONE 'Asia/Seoul')
          AND COALESCE(j.script_json->'wf13_auto'->>'state', '') NOT IN ('running_tts', 'running_video', 'running_publish', 'completed', 'skipped')
          AND NOT EXISTS (
            SELECT 1
            FROM platform_posts p
            WHERE p.job_id = j.id
              AND p.platform = 'youtube'
              AND COALESCE(p.status, '') = 'published'
          )
          AND (COALESCE(j.status, '') <> 'PUBLISH_FAILED' OR COALESCE(j.script_json->'wf13_auto'->>'state', '') IN ('failed', 'blocked'))
        ORDER BY j.created_at ASC, j.id ASC
        """
    )
    return [dict(row) for row in rows]


async def _generate_fixed_seed_tts_for_job(
    *,
    job_id: str,
    existing_job: dict[str, Any],
    script_json: dict[str, Any],
    seed: int,
    avatar_id: str,
    use_avatar_iv_model: bool,
    targets: list[str],
) -> dict[str, Any]:
    body_script_text = _get_tts_script_text(script_json)
    if not body_script_text:
        raise RuntimeError("tts_script_text is missing")
    body_script_text, removed_opening, removed_ending = _strip_fixed_intro_outro_lines(body_script_text)
    if not body_script_text:
        raise RuntimeError("tts body script is empty after removing fixed opening/ending lines")

    tts_attempts: list[dict[str, Any]] = []
    body_audio_bytes: bytes | None = None
    last_tts_error: Optional[Exception] = None
    for attempt_no in range(1, _WF13_TTS_MAX_ATTEMPTS + 1):
        attempt_started_at = datetime.now(timezone.utc)
        try:
            body_audio_bytes = await _generate_tts_audio_content(job_id, body_script_text, seed=seed)
            tts_attempts.append(
                {
                    "attempt_no": attempt_no,
                    "status": "success",
                    "error_type": "",
                    "status_code": 200,
                    "started_at": attempt_started_at.isoformat(),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            break
        except Exception as e:
            detail = str(e)
            status_code = _extract_http_status_code_from_tts_error(detail)
            error_type = _extract_tts_error_type(detail)
            tts_attempts.append(
                {
                    "attempt_no": attempt_no,
                    "status": "failed",
                    "error_type": error_type,
                    "status_code": status_code,
                    "started_at": attempt_started_at.isoformat(),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            last_tts_error = e
            if attempt_no >= _WF13_TTS_MAX_ATTEMPTS or not _wf13_should_retry_tts_error(detail):
                break
            backoff_seconds = _WF13_TTS_RETRY_BACKOFF_SECONDS[min(attempt_no - 1, len(_WF13_TTS_RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "[wf13] retrying TTS job_id=%s attempt=%s/%s wait=%ss err=%s",
                job_id,
                attempt_no,
                _WF13_TTS_MAX_ATTEMPTS,
                backoff_seconds,
                detail,
            )
            await asyncio.sleep(backoff_seconds)
    if body_audio_bytes is None:
        detail = str(last_tts_error or "unknown tts error")
        raise Wf13PipelineError(
            detail,
            error_type=_extract_tts_error_type(detail),
            failure_stage="tts",
            retryable=_wf13_should_retry_tts_error(detail),
            should_stop_workflow=False,
        )

    audio_bytes, concat_attempts, body_audio_spec, final_audio_spec, tts_timing = await _concat_tts_audio_with_fixed_clips(
        body_audio_bytes
    )
    filename = _normalize_audio_filename(job_id, None, script_json)
    stored = put_bytes_and_presign(
        prefix=settings.media_s3_prefix_tts,
        filename=filename,
        content=audio_bytes,
        content_type="audio/wav",
    )
    merged_script = _merge_script_json_with_media_names(
        script_json,
        audio_filename=filename,
        tts_script_text=body_script_text,
        tts_error_type="",
        tts_error_detail="",
    )
    merged_script["selected_tts_seed"] = int(seed)
    merged_script["selected_tts_timing"] = dict(tts_timing or {})
    merged_script["tts_seed_strategy"] = "wf13_fixed"
    merged_script["tts_seed_values"] = [int(seed)]
    merged_script["tts_body_script_text"] = body_script_text
    merged_script["tts_fixed_clips_enabled"] = True
    merged_script["tts_fixed_clips_removed_opening"] = removed_opening
    merged_script["tts_fixed_clips_removed_ending"] = removed_ending
    merged_script["tts_downstream_intent"] = "wf13_auto"
    merged_script = _merge_script_json_with_wf13_state(
        merged_script,
        state="tts_done",
        seed=seed,
        avatar_id=avatar_id,
        use_avatar_iv_model=use_avatar_iv_model,
        targets=targets,
        last_completed_stage="tts",
        last_error="",
        retryable=False,
        tts_attempts=tts_attempts,
    )
    await job_service.update_job(
        job_id,
        audio_url=stored.s3_uri,
        final_url=stored.s3_uri,
        error_message="",
        script_json=merged_script,
    )
    return {
        "audio_s3_uri": stored.s3_uri,
        "audio_url": stored.presigned_url,
        "audio_filename": filename,
        "selected_tts_seed": int(seed),
        "selected_tts_timing": dict(tts_timing or {}),
        "tts_concat_attempts": int(concat_attempts),
        "tts_body_audio_spec": body_audio_spec,
        "tts_final_audio_spec": final_audio_spec,
        "tts_attempts": tts_attempts,
    }


async def _run_wf13_heygen_generate(
    *,
    job_id: str,
    channel_id: str,
    user_id: str,
    audio_url: str,
    avatar_id: str,
    use_avatar_iv_model: bool,
) -> dict[str, Any]:
    if not audio_url:
        raise RuntimeError("audio_url is required")
    if not avatar_id:
        raise RuntimeError("avatar_id is required")

    avatar_payload = await _heygen_get_json(f"/v2/avatar/{avatar_id}/details")
    avatar_data = avatar_payload.get("data") if isinstance(avatar_payload.get("data"), dict) else {}
    avatar_type = str(avatar_data.get("type") or avatar_data.get("avatar", {}).get("type") or "avatar")
    applied_avatar_iv_model = avatar_type == "talking_photo" and bool(use_avatar_iv_model)
    character = (
        {
            "type": "talking_photo",
            "talking_photo_id": avatar_id,
            **({"use_avatar_iv_model": True} if applied_avatar_iv_model else {}),
        }
        if avatar_type == "talking_photo"
        else {
            "type": "avatar",
            "avatar_id": avatar_id,
            "avatar_style": "normal",
        }
    )
    request_snapshot = {
        "avatar_id": avatar_id,
        "use_avatar_iv_model": applied_avatar_iv_model,
        "voice_type": "audio",
        "has_audio_url": bool(audio_url),
        "audio_url_host": (lambda value: (value.split("/")[2] if "://" in value else ""))(audio_url),
        "speed": float(settings.heygen_speed),
        "dimension": {"width": int(settings.heygen_video_width), "height": int(settings.heygen_video_height)},
        "caption": bool(settings.heygen_caption_enabled),
    }
    payload = {
        "video_inputs": [
            {
                "character": character,
                "voice": {
                    "type": "audio",
                    "audio_url": audio_url,
                    "speed": float(settings.heygen_speed),
                },
            }
        ],
        "dimension": {"width": int(settings.heygen_video_width), "height": int(settings.heygen_video_height)},
        "caption": bool(settings.heygen_caption_enabled),
    }
    create_started_at = datetime.now(timezone.utc)
    try:
        response_payload = await _heygen_post_json("/v2/video/generate", payload=payload)
    except Exception as e:
        detail = str(e)
        if _wf13_is_heygen_quota_error(detail):
            raise Wf13PipelineError(
                detail,
                error_type="heygen_insufficient_credit",
                failure_stage="video",
                retryable=False,
                should_stop_workflow=True,
                blocked_reason="heygen_insufficient_credit",
            ) from e
        raise
    video_id = str((response_payload.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError(f"HeyGen create returned no video_id: {response_payload}")

    poll_deadline = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(settings.heygen_max_wait_seconds)))
    video_url = ""
    status_value = "processing"
    poll_payload: dict[str, Any] = {}
    while datetime.now(timezone.utc) < poll_deadline:
        await asyncio.sleep(max(1, int(settings.heygen_poll_interval_seconds)))
        poll_payload = await _heygen_get_json(f"/v1/video_status.get?video_id={video_id}")
        poll_data = poll_payload.get("data") if isinstance(poll_payload.get("data"), dict) else {}
        status_value = str(poll_data.get("status") or "processing").strip().lower()
        if status_value == "completed":
            video_url = str(poll_data.get("video_url") or "").strip()
            break
        if status_value == "failed":
            error_obj = poll_data.get("error")
            detail = f"HeyGen failed: {json.dumps(error_obj, ensure_ascii=False) if error_obj is not None else status_value}"
            if _wf13_is_heygen_quota_error(detail):
                raise Wf13PipelineError(
                    detail,
                    error_type="heygen_insufficient_credit",
                    failure_stage="video",
                    retryable=False,
                    should_stop_workflow=True,
                    blocked_reason="heygen_insufficient_credit",
                )
            raise RuntimeError(detail)
    if not video_url:
        raise RuntimeError(f"HeyGen video generation incomplete: {status_value}")

    heygen_cost_usd, heygen_pricing_kind, heygen_pricing_source = _resolve_heygen_cost(
        None,
        response_payload,
        poll_payload,
    )
    await _record_cost_event_safe(
        job_id=job_id,
        topic_text="",
        stage="video",
        process="heygen_generate",
        provider="heygen",
        attempt_no=1,
        status="success",
        started_at=create_started_at,
        ended_at=datetime.now(timezone.utc),
        usage_json={
            "video_id": video_id,
            "avatar_id": avatar_id,
            "use_avatar_iv_model": applied_avatar_iv_model,
            "status": status_value,
        },
        raw_response_json={
            "request_snapshot": request_snapshot,
            "create_response": response_payload,
            "poll_response": poll_payload,
        },
        cost_usd=heygen_cost_usd,
        pricing_kind=heygen_pricing_kind,
        pricing_source=heygen_pricing_source,
        api_key_family="heygen",
        error_type="",
        error_message="",
        idempotency_key=f"wf13:heygen:{job_id}:{video_id}",
    )
    kst_date = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")
    return {
        "job_id": job_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "video_url": video_url,
        "video_id": video_id,
        "video_filename": f"{kst_date}-{job_id}.mp4",
        "avatar_id": avatar_id,
        "use_avatar_iv_model": applied_avatar_iv_model,
        "status": status_value,
        "request_snapshot": request_snapshot,
        "response_snapshot": poll_payload or response_payload,
    }


async def _finalize_video_auto(
    *,
    job_id: str,
    video_url: str,
    video_filename: str,
    heygen_status: str,
    heygen_video_id: str,
    heygen_avatar_id: str,
    heygen_use_avatar_iv_model: bool,
    heygen_request_snapshot: dict[str, Any],
    heygen_response_snapshot: dict[str, Any],
    seed: int,
    avatar_id: str,
    use_avatar_iv_model: bool,
    targets: list[str],
) -> dict[str, Any]:
    existing_job = await job_service.get_job(job_id)
    if existing_job is None:
        raise RuntimeError("Job not found")
    existing_script_json = _as_script_json(existing_job.get("script_json"))
    normalized_video_filename = _normalize_video_filename(job_id, video_filename, existing_script_json)
    hardburn_video_filename = _build_hardburn_video_filename(normalized_video_filename)
    srt_filename = _build_srt_filename(normalized_video_filename)

    video_resp = await _http_client.get(video_url, timeout=300.0)
    video_resp.raise_for_status()
    raw_stored = put_bytes_and_presign(
        prefix=settings.media_s3_prefix_videos,
        filename=normalized_video_filename,
        content=video_resp.content,
        content_type=video_resp.headers.get("content-type") or "video/mp4",
    )
    subtitle_script_text = _get_subtitle_script_text(existing_script_json)
    tts_script_text = _get_tts_script_text(existing_script_json)
    audio_url = str(existing_job.get("audio_url") or "").strip()
    caption_artifacts = await _build_caption_artifacts(
        job_id=job_id,
        audio_url=audio_url,
        subtitle_script_text=subtitle_script_text,
        tts_script_text=tts_script_text,
    )
    srt_content = str(caption_artifacts.get("srt_content") or "")
    srt_stored = put_bytes_and_presign(
        prefix=settings.media_s3_prefix_srt,
        filename=srt_filename,
        content=srt_content.encode("utf-8"),
        content_type="application/x-subrip; charset=utf-8",
    )
    hardburn_video_bytes = await asyncio.to_thread(
        _render_hardburn_video_sync,
        raw_video_bytes=video_resp.content,
        srt_content=srt_content,
    )
    hardburn_stored = put_bytes_and_presign(
        prefix=settings.media_s3_prefix_videos_with_subtitle,
        filename=hardburn_video_filename,
        content=hardburn_video_bytes,
        content_type="video/mp4",
    )
    generated_content_id, generated_content_error = await _register_generated_content(
        job_id=job_id,
        script_text=subtitle_script_text,
        content_url=hardburn_stored.s3_uri,
    )
    await _record_cost_event_safe(
        job_id=job_id,
        topic_text=str(existing_job.get("concept_text") or ""),
        stage="video",
        process="hardburn_subtitle",
        provider="ffmpeg",
        attempt_no=1,
        status="success",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        usage_json={
            "preview_asset": "hardburn",
            "publish_asset": "hardburn",
            "subtitle_sha256": caption_artifacts.get("subtitle_sha256"),
            "asr_model": caption_artifacts.get("asr_model"),
            "word_count": caption_artifacts.get("word_count"),
            "matched_sentence_count": caption_artifacts.get("matched_sentence_count"),
            "raw_video_bytes": len(video_resp.content),
            "hardburn_video_bytes": len(hardburn_video_bytes),
        },
        raw_response_json={
            "caption_request_json": caption_artifacts.get("request_json") or {},
            "srt_filename": srt_filename,
            "hardburn_video_filename": hardburn_video_filename,
        },
        cost_usd=0.0,
        pricing_kind="estimated",
        pricing_source="provider_usage_estimate",
        api_key_family="hardburn_subtitle",
        error_type="",
        error_message="",
        idempotency_key=f"wf13:hardburn:{job_id}:{hardburn_video_filename}",
    )
    merged_script = _merge_script_json_with_media_names(
        existing_script_json,
        video_filename=hardburn_video_filename,
        generated_content_id=generated_content_id if generated_content_id else None,
        generated_content_error=generated_content_error,
    )
    media_names = merged_script.get("media_names") if isinstance(merged_script.get("media_names"), dict) else {}
    media_names["raw_video_filename"] = normalized_video_filename
    media_names["hardburn_video_filename"] = hardburn_video_filename
    media_names["srt_filename"] = srt_filename
    merged_script["media_names"] = media_names
    merged_script["video_assets"] = {
        "raw_video_s3_uri": raw_stored.s3_uri,
        "hardburn_video_s3_uri": hardburn_stored.s3_uri,
        "srt_s3_uri": srt_stored.s3_uri,
        "preview_asset": "hardburn",
        "publish_asset": "hardburn",
    }
    merged_script["video_hardburn"] = {
        "status": "ready",
        "error": "",
        "raw_video_s3_uri": raw_stored.s3_uri,
        "hardburn_video_s3_uri": hardburn_stored.s3_uri,
        "srt_s3_uri": srt_stored.s3_uri,
        "subtitle_sha256": caption_artifacts.get("subtitle_sha256"),
        "cue_count": caption_artifacts.get("cue_count") or caption_artifacts.get("display_sentence_count"),
        "asr_model": caption_artifacts.get("asr_model"),
        "matched_sentence_count": caption_artifacts.get("matched_sentence_count"),
        "word_count": caption_artifacts.get("word_count"),
    }
    merged_script = _merge_script_json_with_wf13_state(
        merged_script,
        state="video_ready",
        seed=seed,
        avatar_id=avatar_id,
        use_avatar_iv_model=use_avatar_iv_model,
        targets=targets,
        last_completed_stage="video",
        last_error="",
    )
    await job_service.update_job(
        job_id,
        final_url=hardburn_stored.s3_uri,
        video_url=hardburn_stored.s3_uri,
        error_message="",
        script_json=merged_script,
    )
    return {
        "video_url": hardburn_stored.presigned_url,
        "video_s3_uri": hardburn_stored.s3_uri,
        "video_filename": hardburn_video_filename,
        "srt_s3_uri": srt_stored.s3_uri,
        "raw_video_s3_uri": raw_stored.s3_uri,
        "audio_url": _resolve_internal_media_url(audio_url),
        "subtitle_script_text": subtitle_script_text,
        "title": _build_publish_title(job_id, hardburn_video_filename, subtitle_script_text, str(existing_job.get("concept_text") or "")),
        "description": _build_publish_description(subtitle_script_text),
        "caption": _build_publish_caption(subtitle_script_text),
        "heygen_status": heygen_status,
        "heygen_video_id": heygen_video_id,
        "heygen_avatar_id": heygen_avatar_id,
        "heygen_use_avatar_iv_model": heygen_use_avatar_iv_model,
        "heygen_request_snapshot": heygen_request_snapshot,
        "heygen_response_snapshot": heygen_response_snapshot,
    }


async def _publish_auto_video(
    *,
    job_id: str,
    channel_id: str,
    video_url: str,
    audio_url: str,
    video_filename: str,
    title: str,
    description: str,
    caption: str,
    subtitle_script_text: str,
) -> dict[str, Any]:
    service_base_url = settings.sns_publisher_service_url.rstrip("/")
    if not service_base_url:
        raise RuntimeError("SNS_PUBLISHER_SERVICE_URL is not configured")
    await job_service.transition_status(job_id, "PUBLISHING")
    resp = await _http_client.post(
        f"{service_base_url}/publish",
        json={
            "job_id": job_id,
            "video_url": video_url,
            "audio_url": audio_url,
            "targets": ["youtube"],
            "title": title,
            "description": description,
            "caption": caption,
            "subtitle_script_text": subtitle_script_text,
            "video_filename": video_filename,
        },
        timeout=300.0,
    )
    resp.raise_for_status()
    payload = resp.json() if resp.content else {}
    final_status = str(payload.get("final_status") or "PUBLISH_FAILED").strip() or "PUBLISH_FAILED"
    summary_text = str(payload.get("summary_text") or "").strip()
    await job_service.transition_status(job_id, final_status)
    return {
        "final_status": final_status,
        "summary_text": summary_text,
        "response_json": payload,
    }


async def _wf13_publish_auto_with_retry(
    *,
    job_id: str,
    channel_id: str,
    video_url: str,
    audio_url: str,
    video_filename: str,
    title: str,
    description: str,
    caption: str,
    subtitle_script_text: str,
    script_json: dict[str, Any],
    seed: int,
    avatar_id: str,
    use_avatar_iv_model: bool,
    targets: list[str],
) -> dict[str, Any]:
    publish_attempts: list[dict[str, Any]] = []
    last_error_message = ""
    last_status_code: Optional[int] = None

    async def _persist_publish_attempts() -> None:
        merged_script = _merge_script_json_with_wf13_state(
            script_json,
            state="running_publish",
            seed=seed,
            avatar_id=avatar_id,
            use_avatar_iv_model=use_avatar_iv_model,
            targets=targets,
            last_error="",
        )
        wf13_state = _get_wf13_state(merged_script)
        wf13_state["publish_attempts"] = publish_attempts
        merged_script["wf13_auto"] = wf13_state
        await job_service.update_job(job_id, script_json=merged_script, error_message="")

    for attempt_no in range(1, _WF13_PUBLISH_MAX_ATTEMPTS + 1):
        attempt_started_at = datetime.now(timezone.utc)
        try:
            result = await _publish_auto_video(
                job_id=job_id,
                channel_id=channel_id,
                video_url=video_url,
                audio_url=audio_url,
                video_filename=video_filename,
                title=title,
                description=description,
                caption=caption,
                subtitle_script_text=subtitle_script_text,
            )
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response is not None else None
            detail = str(e)
            publish_attempts.append(
                {
                    "attempt_no": attempt_no,
                    "status": "failed",
                    "status_code": status_code,
                    "error_message": _clip(detail, limit=220),
                    "started_at": attempt_started_at.isoformat(),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await _persist_publish_attempts()
            last_error_message = detail
            last_status_code = status_code
            if attempt_no >= _WF13_PUBLISH_MAX_ATTEMPTS or not _wf13_is_transient_publish_error(detail, status_code=status_code):
                break
            backoff_seconds = _WF13_PUBLISH_RETRY_BACKOFF_SECONDS[min(attempt_no - 1, len(_WF13_PUBLISH_RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "[wf13] retrying publish job_id=%s attempt=%s/%s wait=%ss err=%s",
                job_id,
                attempt_no,
                _WF13_PUBLISH_MAX_ATTEMPTS,
                backoff_seconds,
                detail,
            )
            await asyncio.sleep(backoff_seconds)
            continue
        except httpx.RequestError as e:
            detail = str(e)
            publish_attempts.append(
                {
                    "attempt_no": attempt_no,
                    "status": "failed",
                    "status_code": None,
                    "error_message": _clip(detail, limit=220),
                    "started_at": attempt_started_at.isoformat(),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await _persist_publish_attempts()
            last_error_message = detail
            last_status_code = None
            if attempt_no >= _WF13_PUBLISH_MAX_ATTEMPTS:
                break
            backoff_seconds = _WF13_PUBLISH_RETRY_BACKOFF_SECONDS[min(attempt_no - 1, len(_WF13_PUBLISH_RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "[wf13] retrying publish request job_id=%s attempt=%s/%s wait=%ss err=%s",
                job_id,
                attempt_no,
                _WF13_PUBLISH_MAX_ATTEMPTS,
                backoff_seconds,
                detail,
            )
            await asyncio.sleep(backoff_seconds)
            continue

        detail = _wf13_extract_publish_error(result.get("response_json") if isinstance(result.get("response_json"), dict) else {})
        final_status = str(result.get("final_status") or "").strip().upper()
        publish_attempts.append(
            {
                "attempt_no": attempt_no,
                "status": "success" if final_status == "PUBLISHED" else "failed",
                "status_code": None,
                "error_message": _clip(detail, limit=220),
                "started_at": attempt_started_at.isoformat(),
                "ended_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await _persist_publish_attempts()
        if final_status == "PUBLISHED":
            return result
        if _wf13_is_upload_limit_error(detail):
            raise Wf13PipelineError(
                detail or "YouTube upload limit exceeded",
                error_type="youtube_upload_limit_exceeded",
                failure_stage="publish",
                retryable=False,
                should_stop_workflow=False,
            )
        if attempt_no >= _WF13_PUBLISH_MAX_ATTEMPTS or not _wf13_is_transient_publish_error(detail):
            raise Wf13PipelineError(
                detail or "YouTube publish failed",
                error_type="youtube_publish_failed",
                failure_stage="publish",
                retryable=False,
                should_stop_workflow=False,
            )
        backoff_seconds = _WF13_PUBLISH_RETRY_BACKOFF_SECONDS[min(attempt_no - 1, len(_WF13_PUBLISH_RETRY_BACKOFF_SECONDS) - 1)]
        logger.warning(
            "[wf13] retrying publish result job_id=%s attempt=%s/%s wait=%ss err=%s",
            job_id,
            attempt_no,
            _WF13_PUBLISH_MAX_ATTEMPTS,
            backoff_seconds,
            detail,
        )
        await asyncio.sleep(backoff_seconds)

    raise Wf13PipelineError(
        last_error_message or "YouTube publish failed",
        error_type="youtube_publish_failed",
        failure_stage="publish",
        retryable=False,
        should_stop_workflow=False,
    )


async def _resolve_heygen_avatar_id(_: dict) -> tuple[str, str]:
    # 기본 avatar fallback은 env 첫 번째 항목을 사용한다.
    # 실제 WF-12 승인 경로에서는 반드시 사용자가 선택한 avatar를 사용한다.
    options = _parse_heygen_avatar_options_from_env()
    first = options[0]
    return str(first["avatar_id"]), f"env:{first['label']}"


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


_SCRIPT_MIN_CHARS = 280
_SCRIPT_MAX_CHARS = 350
_SCRIPT_REWRITE_MAX_ATTEMPTS = 5
_REPORT_SCRIPT_RETRY_MAX_ATTEMPTS = 30


def _script_char_count(script_text: str) -> int:
    # 줄바꿈은 포맷 요소라 길이 계산에서 제외하고, 실제 문장 길이만 본다.
    return len((script_text or "").replace("\r", "").replace("\n", ""))


def _sanitize_prompt_text(text: str) -> str:
    sanitized = (text or "").replace("\x00", "")
    return sanitized.encode("utf-8", "ignore").decode("utf-8")


def _split_script_sentences(script_text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", (script_text or "").replace("\n", " ")).strip()
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    return [part.strip() for part in parts if part.strip()]


def _split_caption_alignment_sentences(script_text: str) -> list[str]:
    normalized = " ".join((script_text or "").replace("\r", "\n").split())
    if not normalized:
        return []
    parts = re.findall(r"[^.!?]+[.!?]?", normalized)
    return [part.strip() for part in parts if part and part.strip()]


def _build_fixed_caption_text(body_text: str) -> str:
    body_lines = [line.strip() for line in (body_text or "").splitlines() if line.strip()]
    if not body_lines:
        return ""
    output_lines = list(body_lines)
    if output_lines[0] != TTS_SCRIPT_OPENING_LINE:
        output_lines.insert(0, TTS_SCRIPT_OPENING_LINE)
    if output_lines[-1] != SCRIPT_ENDING_LINE:
        output_lines.append(SCRIPT_ENDING_LINE)
    return "\n".join(output_lines).strip()


def _clip(text: str, *, limit: int = 260) -> str:
    value = _sanitize_prompt_text((text or "").strip())
    if not value:
        return ""
    return value if len(value) <= max(1, int(limit)) else value[: max(1, int(limit))]


def _build_tts_request_body(script_text: str, *, seed: Optional[int] = None) -> dict:
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
    if seed is not None:
        tts_body["seed"] = int(seed)

    return tts_body


def _classify_tts_error(detail: str, *, status_code: Optional[int] = None) -> str:
    normalized = (detail or "").lower()
    network_markers = (
        "name or service not known",
        "temporary failure in name resolution",
        "network connection lost",
        "connecterror",
        "connection refused",
        "connection reset",
        "socket hang up",
        "upstream connect error",
        "temporarily unavailable",
        "timed out",
        "readtimeout",
        "connecttimeout",
        "nodename nor servname provided",
        "network is unreachable",
        "no route to host",
    )
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

    if any(marker in normalized for marker in network_markers):
        return "tts_network_error"
    if any(marker in normalized for marker in runtime_markers):
        return "tts_server_runtime_error"
    if any(marker in normalized for marker in request_markers):
        return "request_validation_error"
    if status_code in {408, 429, 502, 503, 504, 524}:
        return "tts_network_error"
    if status_code is not None and status_code >= 500:
        return "tts_server_runtime_error"
    if status_code in {400, 401, 403, 404, 405, 409, 422}:
        return "request_validation_error"
    return "tts_server_runtime_error"


def _extract_tts_error_type(detail: str) -> str:
    match = re.search(r"\[(request_validation_error|tts_server_runtime_error|tts_network_error)\]", detail or "")
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


def _validate_subtitle_script(
    tts_script_text: str,
    subtitle_script_text: str,
    *,
    raw_report_text: str = "",
    enforce_difference: bool = False,
) -> None:
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
    if enforce_difference and subtitle_script_text.strip() == tts_script_text.strip():
        raise RuntimeError("subtitle_script_text must not be identical to tts_script_text")
    if TTS_SCRIPT_OPENING_LINE in subtitle_script_text:
        raise RuntimeError("subtitle_script_text must not include fixed opening line")
    if SCRIPT_ENDING_LINE in subtitle_script_text:
        raise RuntimeError("subtitle_script_text must not include fixed ending line")
    report_has_alnum = bool(re.search(r"[A-Za-z0-9]", raw_report_text or ""))
    subtitle_has_alnum = bool(re.search(r"[A-Za-z0-9]", subtitle_script_text or ""))
    if report_has_alnum and not subtitle_has_alnum:
        raise RuntimeError("subtitle_script_text must preserve numeric/alphabetic notation from report")
    spoken_sentence_count = len(_split_caption_alignment_sentences(_build_fixed_caption_text(tts_script_text)))
    display_sentence_count = len(_split_caption_alignment_sentences(_build_fixed_caption_text(subtitle_script_text)))
    if spoken_sentence_count != display_sentence_count:
        raise RuntimeError(
            "caption sentence count mismatch after fixed opening/ending: "
            f"spoken={spoken_sentence_count} display={display_sentence_count}"
        )


def _validate_tts_script(raw_report_text: str, tts_script_text: str) -> str:
    warning_message = ""
    keywords = _extract_topic_keywords(raw_report_text)
    if keywords:
        tts_ok = any(keyword in tts_script_text for keyword in keywords)
        if not tts_ok:
            drift_message = f"script rewrite topic drift detected; missing keywords={keywords}"
            if settings.script_rewrite_topic_keyword_guard_enabled:
                raise RuntimeError(drift_message)
            warning_message = drift_message
            logger.warning(
                "[script-rewrite] keyword drift ignored by config (SCRIPT_REWRITE_TOPIC_KEYWORD_GUARD_ENABLED=false): %s",
                drift_message,
            )
    tts_len = _script_char_count(tts_script_text)
    if not (_SCRIPT_MIN_CHARS <= tts_len <= _SCRIPT_MAX_CHARS):
        raise RuntimeError(
            f"tts_script_text length out of range: {tts_len} (expected {_SCRIPT_MIN_CHARS}-{_SCRIPT_MAX_CHARS})"
        )
    sentences = _split_script_sentences(tts_script_text)
    if len(sentences) < 6:
        raise RuntimeError("tts_script_text must contain at least 6 sentences")
    tts_lines = [line.strip() for line in tts_script_text.splitlines() if line.strip()]
    if not tts_lines:
        raise RuntimeError("tts_script_text is empty")
    if TTS_SCRIPT_OPENING_LINE in tts_script_text:
        raise RuntimeError("tts_script_text must not include fixed opening line")
    if SCRIPT_ENDING_LINE in tts_script_text:
        raise RuntimeError("tts_script_text must not include fixed ending line")
    return warning_message


def _resolve_openai_api_key_for_rewrite() -> str:
    primary = (settings.openai_api_key_rewrite or "").strip()
    if primary:
        return primary
    fallback = (settings.openai_fallback_api_key or "").strip()
    if fallback:
        return fallback
    legacy = os.environ.get("OPENAI_API_KEY", "").strip()
    if legacy:
        return legacy
    raise RuntimeError(
        "OPENAI_API_KEY_REWRITE or OPENAI_FALLBACK_API_KEY "
        "(or legacy OPENAI_API_KEY) is not configured for script rewrite"
    )


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
    api_key = _resolve_openai_api_key_for_rewrite()

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
            request_started_at = datetime.now(timezone.utc)
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
            usage_payload: dict[str, Any] = {}
            if response.usage is not None:
                usage_payload = {
                    "prompt_tokens": int(getattr(response.usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0),
                    "total_tokens": int(getattr(response.usage, "total_tokens", 0) or 0),
                    "model": settings.script_rewrite_model,
                }
            estimated_cost = _estimate_script_rewrite_cost_usd(usage_payload)
            await _record_cost_event_safe(
                job_id=prompt_log.get("job_id", ""),
                topic_text=str((prompt_log.get("job") or {}).get("concept_text") or ""),
                stage="script",
                process="tts_script_rewrite",
                provider="openai",
                attempt_no=int(attempt_record["attempt"]),
                status="success" if bool(response.choices) else "failed",
                started_at=request_started_at,
                ended_at=datetime.now(timezone.utc),
                usage_json=usage_payload,
                raw_response_json={"prompt": tts_prompt, "has_choices": bool(response.choices)},
                cost_usd=estimated_cost,
                pricing_kind=_estimated_pricing_kind(estimated_cost),
                pricing_source=_estimated_pricing_source(estimated_cost),
                api_key_family="rewrite",
                error_type="",
                error_message="",
                idempotency_key=f"script:tts:{prompt_log.get('job_id','')}:{attempt_record['attempt']}",
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
                warning_message = _validate_tts_script(raw_report_text, tts_script_text)
                if warning_message:
                    attempt_record["validation_warning"] = warning_message
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
        subtitle_validation_error = ""
        for attempt in range(max(1, max_attempts)):
            use_retry_prompt = attempt > 0 or bool(subtitle_script_text)
            subtitle_prompt = (
                build_subtitle_retry_prompt(
                    raw_report_text=raw_report_text,
                    tts_script_text=tts_script_text,
                    previous_script_text=subtitle_script_text,
                    char_count=_script_char_count(subtitle_script_text),
                    validation_error=subtitle_validation_error,
                )
                if use_retry_prompt
                else build_subtitle_from_tts_prompt(
                    tts_script_text=tts_script_text,
                    raw_report_text=raw_report_text,
                )
            )
            subtitle_prompt = _sanitize_prompt_text(subtitle_prompt)
            attempt_record = {
                "attempt": len(prompt_log["rewrite"]["subtitle_attempts"]) + 1,
                "prompt": subtitle_prompt,
                "response_text": "",
                "char_count": 0,
                "validation_error": "",
            }
            request_started_at = datetime.now(timezone.utc)
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
            usage_payload: dict[str, Any] = {}
            if response.usage is not None:
                usage_payload = {
                    "prompt_tokens": int(getattr(response.usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0),
                    "total_tokens": int(getattr(response.usage, "total_tokens", 0) or 0),
                    "model": settings.script_rewrite_model,
                }
            estimated_cost = _estimate_script_rewrite_cost_usd(usage_payload)
            await _record_cost_event_safe(
                job_id=prompt_log.get("job_id", ""),
                topic_text=str((prompt_log.get("job") or {}).get("concept_text") or ""),
                stage="script",
                process="subtitle_script_rewrite",
                provider="openai",
                attempt_no=int(attempt_record["attempt"]),
                status="success" if bool(response.choices) else "failed",
                started_at=request_started_at,
                ended_at=datetime.now(timezone.utc),
                usage_json=usage_payload,
                raw_response_json={"prompt": subtitle_prompt, "has_choices": bool(response.choices)},
                cost_usd=estimated_cost,
                pricing_kind=_estimated_pricing_kind(estimated_cost),
                pricing_source=_estimated_pricing_source(estimated_cost),
                api_key_family="rewrite",
                error_type="",
                error_message="",
                idempotency_key=f"script:subtitle:{prompt_log.get('job_id','')}:{attempt_record['attempt']}",
            )
            if not response.choices:
                last_error = RuntimeError("subtitle rewrite returned no choices")
                subtitle_validation_error = str(last_error)
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
                subtitle_validation_error = str(last_error)
                attempt_record["validation_error"] = str(last_error)
                prompt_log["rewrite"]["subtitle_attempts"].append(attempt_record)
                continue
            try:
                _validate_subtitle_script(
                    tts_script_text,
                    subtitle_script_text,
                    raw_report_text=raw_report_text,
                    enforce_difference=False,
                )
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
                subtitle_validation_error = str(e)
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


def _upload_subtitle_report_file(
    *,
    job_id: str,
    filename: str,
    file_bytes: bytes,
) -> tuple[object | None, Exception | None, bool]:
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
        logger.error("[storage] report upload failed job_id=%s: %s", job_id, e)

    attachment_limit = _discord_attachment_limit_bytes()
    is_link_only_report = stored.size_bytes > attachment_limit if stored else len(file_bytes) > attachment_limit
    return stored, upload_error, is_link_only_report


def _resolve_tts_fixed_clip_paths() -> tuple[str, str]:
    opening_path = (settings.tts_opening_audio_path or "").strip()
    ending_path = (settings.tts_ending_audio_path or "").strip()
    if not opening_path or not ending_path:
        raise RuntimeError("TTS_OPENING_AUDIO_PATH and TTS_ENDING_AUDIO_PATH must be configured")
    if not os.path.isfile(opening_path):
        raise RuntimeError(f"TTS opening clip not found: {opening_path}")
    if not os.path.isfile(ending_path):
        raise RuntimeError(f"TTS ending clip not found: {ending_path}")
    return opening_path, ending_path


def _ffprobe_audio_spec_sync(audio_path: str) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,sample_fmt,bits_per_sample",
        "-of",
        "json",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"ffprobe failed for {audio_path}: {stderr or 'unknown error'}")
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception as e:
        raise RuntimeError(f"ffprobe returned invalid json for {audio_path}: {e}") from e
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError(f"ffprobe returned no audio streams for {audio_path}")
    stream = streams[0]
    codec_name = str(stream.get("codec_name") or "").strip()
    sample_fmt = str(stream.get("sample_fmt") or "").strip()
    try:
        sample_rate = int(str(stream.get("sample_rate") or "0"))
    except Exception:
        sample_rate = 0
    try:
        channels = int(str(stream.get("channels") or "0"))
    except Exception:
        channels = 0
    if not codec_name or sample_rate <= 0 or channels <= 0:
        raise RuntimeError(f"invalid audio spec from ffprobe for {audio_path}: {stream}")
    return {
        "codec_name": codec_name,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_fmt": sample_fmt,
    }


def _ffprobe_audio_duration_sync(audio_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"ffprobe duration failed for {audio_path}: {stderr or 'unknown error'}")
    try:
        duration = float((result.stdout or "").strip())
    except Exception as e:
        raise RuntimeError(f"ffprobe returned invalid duration for {audio_path}: {e}") from e
    if duration <= 0.0:
        raise RuntimeError(f"ffprobe returned non-positive duration for {audio_path}: {duration}")
    return duration


def _ffprobe_video_dimensions_sync(video_path: str) -> tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"ffprobe video dimensions failed for {video_path}: {stderr or 'unknown error'}")
    try:
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        stream = streams[0] if streams else {}
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except Exception as e:
        raise RuntimeError(f"ffprobe video dimensions returned invalid json for {video_path}: {e}") from e
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid video dimensions for {video_path}: {payload}")
    return width, height


def _subtitles_filter_arg(*, srt_path: str, width: int, height: int) -> str:
    fontsdir = _ffmpeg_filter_escape(_resolve_hardburn_font_dir())
    style = _HARDBURN_FORCE_STYLE.replace("'", r"\'")
    srt_value = _ffmpeg_filter_escape(srt_path)
    return (
        f"subtitles=filename='{srt_value}':fontsdir='{fontsdir}':"
        f"original_size={width}x{height}:force_style='{style}'"
    )


def _render_hardburn_video_sync(*, raw_video_bytes: bytes, srt_content: str) -> bytes:
    if not raw_video_bytes:
        raise RuntimeError("empty raw video bytes")
    if not srt_content.strip():
        raise RuntimeError("empty srt content")

    with tempfile.TemporaryDirectory(prefix="video-hardburn-") as workdir:
        raw_path = os.path.join(workdir, "raw.mp4")
        srt_path = os.path.join(workdir, "captions.srt")
        output_path = os.path.join(workdir, "hardburn.mp4")
        with open(raw_path, "wb") as f:
            f.write(raw_video_bytes)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        width, height = _ffprobe_video_dimensions_sync(raw_path)
        filter_arg = _subtitles_filter_arg(srt_path=srt_path, width=width, height=height)
        base_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            raw_path,
            "-vf",
            filter_arg,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
        attempts = (
            base_cmd + ["-c:a", "copy", output_path],
            base_cmd + ["-c:a", "aac", "-b:a", "192k", output_path],
        )
        last_error = ""
        for cmd in attempts:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                with open(output_path, "rb") as f:
                    return f.read()
            last_error = (result.stderr or "").strip() or "unknown error"
        raise RuntimeError(f"ffmpeg hardburn failed: {last_error}")


def _normalize_clip_to_spec_sync(
    *,
    src_path: str,
    dst_path: str,
    sample_rate: int,
    channels: int,
    codec_name: str,
    sample_fmt: str = "",
) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        src_path,
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
    ]
    if sample_fmt:
        cmd.extend(["-sample_fmt", sample_fmt])
    cmd.extend(["-c:a", codec_name, dst_path])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"ffmpeg normalize failed for {src_path}: {stderr or 'unknown error'}")


def _build_silence_clip_to_spec_sync(
    *,
    dst_path: str,
    seconds: float,
    sample_rate: int,
    channels: int,
    codec_name: str,
    sample_fmt: str = "",
) -> None:
    safe_seconds = max(0.0, float(seconds))
    if safe_seconds <= 0.0:
        raise RuntimeError("silence seconds must be > 0")
    channel_layout = "mono" if int(channels) == 1 else "stereo"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={int(sample_rate)}:cl={channel_layout}",
        "-t",
        f"{safe_seconds:.3f}",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
    ]
    if sample_fmt:
        cmd.extend(["-sample_fmt", sample_fmt])
    cmd.extend(["-c:a", codec_name, dst_path])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"ffmpeg silence generation failed: {stderr or 'unknown error'}")


def _concat_tts_audio_with_fixed_clips_sync(
    body_audio_bytes: bytes,
) -> tuple[bytes, int, dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not body_audio_bytes:
        raise RuntimeError("empty body audio")

    opening_path, ending_path = _resolve_tts_fixed_clip_paths()
    retries = max(0, int(settings.tts_concat_retries))
    opening_gap_seconds = max(0.0, float(getattr(settings, "tts_opening_gap_seconds", 0.5) or 0.0))
    ending_gap_seconds = max(0.0, float(getattr(settings, "tts_ending_gap_seconds", 0.5) or 0.0))
    max_attempts = 1 + retries
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            with tempfile.TemporaryDirectory(prefix="tts-concat-") as workdir:
                body_path = os.path.join(workdir, "body.wav")
                opening_norm_path = os.path.join(workdir, "opening.norm.wav")
                ending_norm_path = os.path.join(workdir, "ending.norm.wav")
                opening_gap_path = os.path.join(workdir, "opening.gap.wav")
                ending_gap_path = os.path.join(workdir, "ending.gap.wav")
                concat_list_path = os.path.join(workdir, "concat.txt")
                output_path = os.path.join(workdir, "merged.wav")
                with open(body_path, "wb") as f:
                    f.write(body_audio_bytes)

                body_spec = _ffprobe_audio_spec_sync(body_path)
                # body(본문 TTS)의 원본 스펙을 기준으로 opening/ending만 맞춘다.
                body_codec = str(body_spec.get("codec_name") or "pcm_s16le")
                target_codec = body_codec if body_codec.startswith("pcm_") else "pcm_s16le"
                target_sample_fmt = str(body_spec.get("sample_fmt") or "")
                _normalize_clip_to_spec_sync(
                    src_path=opening_path,
                    dst_path=opening_norm_path,
                    sample_rate=int(body_spec["sample_rate"]),
                    channels=int(body_spec["channels"]),
                    codec_name=target_codec,
                    sample_fmt=target_sample_fmt,
                )
                _normalize_clip_to_spec_sync(
                    src_path=ending_path,
                    dst_path=ending_norm_path,
                    sample_rate=int(body_spec["sample_rate"]),
                    channels=int(body_spec["channels"]),
                    codec_name=target_codec,
                    sample_fmt=target_sample_fmt,
                )
                opening_duration_sec = _ffprobe_audio_duration_sync(opening_norm_path)
                body_duration_sec = _ffprobe_audio_duration_sync(body_path)
                ending_duration_sec = _ffprobe_audio_duration_sync(ending_norm_path)

                concat_inputs: list[str] = [opening_norm_path]
                if opening_gap_seconds > 0.0:
                    _build_silence_clip_to_spec_sync(
                        dst_path=opening_gap_path,
                        seconds=opening_gap_seconds,
                        sample_rate=int(body_spec["sample_rate"]),
                        channels=int(body_spec["channels"]),
                        codec_name=target_codec,
                        sample_fmt=target_sample_fmt,
                    )
                    concat_inputs.append(opening_gap_path)
                concat_inputs.append(body_path)
                if ending_gap_seconds > 0.0:
                    _build_silence_clip_to_spec_sync(
                        dst_path=ending_gap_path,
                        seconds=ending_gap_seconds,
                        sample_rate=int(body_spec["sample_rate"]),
                        channels=int(body_spec["channels"]),
                        codec_name=target_codec,
                        sample_fmt=target_sample_fmt,
                    )
                    concat_inputs.append(ending_gap_path)
                concat_inputs.append(ending_norm_path)

                with open(concat_list_path, "w", encoding="utf-8") as concat_file:
                    for p in concat_inputs:
                        concat_file.write(f"file '{p}'\n")

                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    concat_list_path,
                    "-c",
                    "copy",
                    output_path,
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    stderr = (result.stderr or "").strip()
                    raise RuntimeError(f"ffmpeg concat failed (attempt {attempt}/{max_attempts}): {stderr or 'unknown error'}")
                final_spec = _ffprobe_audio_spec_sync(output_path)
                if (
                    int(final_spec.get("sample_rate") or 0) != int(body_spec.get("sample_rate") or 0)
                    or int(final_spec.get("channels") or 0) != int(body_spec.get("channels") or 0)
                    or str(final_spec.get("codec_name") or "").strip() != target_codec
                ):
                    raise RuntimeError(
                        "concat output spec mismatch: "
                        f"body={body_spec} final={final_spec} target_codec={target_codec}"
                    )
                with open(output_path, "rb") as f:
                    merged_audio = f.read()
                if not merged_audio:
                    raise RuntimeError(f"ffmpeg concat produced empty audio (attempt {attempt}/{max_attempts})")
                final_duration_sec = _ffprobe_audio_duration_sync(output_path)
                body_start_sec = opening_duration_sec + opening_gap_seconds
                body_end_sec = body_start_sec + body_duration_sec
                ending_start_sec = body_end_sec + ending_gap_seconds
                tts_timing = {
                    "opening_duration_sec": round(opening_duration_sec, 6),
                    "opening_gap_sec": round(opening_gap_seconds, 6),
                    "body_duration_sec": round(body_duration_sec, 6),
                    "ending_gap_sec": round(ending_gap_seconds, 6),
                    "ending_duration_sec": round(ending_duration_sec, 6),
                    "body_start_sec": round(body_start_sec, 6),
                    "body_end_sec": round(body_end_sec, 6),
                    "ending_start_sec": round(ending_start_sec, 6),
                    "final_duration_sec": round(final_duration_sec, 6),
                }
                return merged_audio, attempt, body_spec, final_spec, tts_timing
        except Exception as e:
            last_error = e
            logger.warning("[tts-concat] attempt=%s/%s failed: %s", attempt, max_attempts, e)

    raise RuntimeError(f"TTS fixed-clip concat failed after {max_attempts} attempts: {last_error}")


async def _concat_tts_audio_with_fixed_clips(
    body_audio_bytes: bytes,
) -> tuple[bytes, int, dict[str, Any], dict[str, Any], dict[str, Any]]:
    return await asyncio.to_thread(_concat_tts_audio_with_fixed_clips_sync, body_audio_bytes)


def _build_tts_variant_caption(variant_index: int, seed: Optional[int], *, concat_attempts: int) -> str:
    seed_text = f" (seed={seed})" if seed is not None else ""
    return (
        f"🔊 TTS 후보 {variant_index + 1}/{_TTS_VARIANT_COUNT}{seed_text}입니다.\n"
        f"고정 오프닝/엔딩 결합 완료 (concat attempt={concat_attempts}).\n"
        "마음에 들면 아래 버튼으로 이 버전을 선택하세요."
    )


def _build_tts_variant_control_caption(
    *,
    success_count: int,
    failure_count: int,
    downstream_intent: str,
    seed_strategy: str,
    seeds: list[int],
) -> str:
    base = [f"🔊 TTS 후보 생성 완료: {success_count}/{_TTS_VARIANT_COUNT}개 성공"]
    if seeds:
        base.append(f"seed 전략: {seed_strategy} / seed={', '.join(str(seed) for seed in seeds)}")
    if failure_count:
        base.append(f"실패: {failure_count}개")
    if downstream_intent == "video_prepare":
        base.append("원하는 후보를 선택한 뒤 일반 승인 또는 고화질 승인을 진행하면 영상 생성으로 이어집니다.")
    else:
        base.append("원하는 후보를 선택한 뒤 일반 승인 또는 고화질 승인을 진행하세요.")
    base.append("마음에 들지 않으면 `다시 생성`으로 새 후보 3개를 만들 수 있습니다.")
    return "\n".join(base)


def _extract_http_status_code_from_tts_error(detail: str) -> Optional[int]:
    match = re.search(r":\s*(\d{3})\b", detail or "")
    if not match:
        return None
    try:
        status_code = int(match.group(1))
    except Exception:
        return None
    return status_code if 100 <= status_code <= 599 else None


def _build_tts_variant_failure_entry(*, variant_index: int, seed: Optional[int], error: Exception) -> dict:
    detail = str(error or "").strip()
    error_type = _extract_tts_error_type(detail)
    status_code = _extract_http_status_code_from_tts_error(detail)
    snippet = _clip(detail, limit=240)
    return {
        "variant_index": int(variant_index),
        "seed": int(seed) if seed is not None else None,
        "error_type": error_type,
        "status_code": status_code,
        "error_snippet": snippet,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_tts_failure_summary(failures: list[dict], *, limit: int = 3) -> str:
    if not failures:
        return ""
    chunks: list[str] = []
    for item in failures[: max(1, limit)]:
        idx = item.get("variant_index")
        seed = item.get("seed")
        error_type = str(item.get("error_type") or "tts_server_runtime_error")
        status_code = item.get("status_code")
        snippet = str(item.get("error_snippet") or "")
        status_text = f"http={status_code}" if status_code else "http=?"
        chunks.append(
            f"v{(int(idx) + 1) if isinstance(idx, int) else '?'} seed={seed} [{error_type}] {status_text} {snippet}"
        )
    return "; ".join(chunks)


def _build_selected_tts_caption(
    *,
    downstream_intent: str,
    variant_index: int,
    selected_seed: Optional[int],
    selected_avatar_label: str,
) -> str:
    seed_text = f"(seed={selected_seed}) " if selected_seed is not None else ""
    avatar_line = (
        f"선택 아바타: {selected_avatar_label}"
        if selected_avatar_label
        else "먼저 아바타 버튼을 선택하세요."
    )
    if downstream_intent == "video_prepare":
        return (
            f"✅ {seed_text}TTS 후보 {variant_index + 1}번을 선택했습니다.\n"
            f"{avatar_line}\n"
            "이제 일반 승인 또는 고화질 승인을 선택한 뒤 최종 확인을 진행하세요."
        )
    return (
        f"✅ {seed_text}TTS 후보 {variant_index + 1}번을 선택했습니다.\n"
        f"{avatar_line}\n"
        "아래에서 일반 승인 또는 고화질 승인을 선택한 뒤 최종 확인을 진행하세요."
    )


async def _disable_tts_variant_buttons(channel_id: str, script_json: dict) -> None:
    message_ids: list[str] = []
    control_message_id = str(script_json.get("tts_variant_control_message_id") or "").strip()
    if control_message_id:
        message_ids.append(control_message_id)
    for variant in _get_tts_variants(script_json):
        message_id = str(variant.get("discord_message_id") or "").strip()
        if message_id:
            message_ids.append(message_id)
    for message_id in message_ids:
        try:
            await _discord_adapter.clear_message_components(channel_id, message_id)
        except Exception as e:
            logger.warning("[discord] clear tts variant buttons failed channel=%s message_id=%s err=%s", channel_id, message_id, e)


async def _store_and_send_tts_variant(
    *,
    job_id: str,
    channel_id: str,
    batch_id: str,
    variant_index: int,
    seed: Optional[int],
    concat_attempts: int,
    audio_bytes: bytes,
    tts_timing: dict[str, Any],
) -> dict:
    filename = _build_tts_variant_filename(job_id, variant_index)
    stored = None
    try:
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_tts,
            filename=filename,
            content=audio_bytes,
            content_type="audio/wav",
        )
    except Exception as e:
        logger.error("[storage] tts variant upload failed job_id=%s variant=%s: %s", job_id, variant_index, e)

    caption = _build_tts_variant_caption(variant_index, seed, concat_attempts=concat_attempts)
    delivery_url = ""
    if stored and stored.size_bytes > _discord_attachment_limit_bytes():
        message_id = await _discord_adapter.send_tts_variant_link_message(
            channel_id=channel_id,
            job_id=job_id,
            batch_id=batch_id,
            variant_index=variant_index,
            caption=caption,
            audio_url=stored.presigned_url,
        )
        delivery_url = stored.presigned_url
    else:
        if stored is None and len(audio_bytes) > _discord_attachment_limit_bytes():
            raise RuntimeError("TTS upload failed and audio exceeds Discord attachment size limit")
        message_id, attachment_url = await _discord_adapter.send_tts_variant_audio_message(
            channel_id=channel_id,
            job_id=job_id,
            batch_id=batch_id,
            variant_index=variant_index,
            caption=caption,
            audio_bytes=audio_bytes,
            filename=filename,
        )
        delivery_url = attachment_url or (stored.presigned_url if stored else "")

    return {
        "variant_index": variant_index,
        "filename": filename,
        "s3_uri": stored.s3_uri if stored else "",
        "attachment_or_presigned_url": delivery_url,
        "discord_message_id": message_id,
        "status": "ready",
        "tts_concat_applied": True,
        "tts_concat_attempts": int(concat_attempts),
        "tts_timing": dict(tts_timing or {}),
    }


async def _generate_tts_audio_content(job_id: str, script_text: str, *, seed: Optional[int] = None) -> bytes:
    # gateway는 내부 router를 우선 사용하고, 비어 있으면 기존 TTS API URL로 fallback 한다.
    tts_api_url = (settings.tts_router_url or "").strip() or settings.tts_api_url.strip()
    if not tts_api_url:
        raise RuntimeError("TTS router URL is not configured")

    tts_body = _build_tts_request_body(script_text, seed=seed)

    request_started_at = datetime.now(timezone.utc)
    call_nonce = int(request_started_at.timestamp() * 1000)
    estimated_cost = _estimate_tts_cost_usd(script_text)
    async with httpx.AsyncClient(timeout=180.0) as client:
        endpoint = tts_api_url if tts_api_url.rstrip("/").endswith("/tts") else f"{tts_api_url.rstrip('/')}/tts"
        try:
            resp = await client.post(
                endpoint,
                json=tts_body,
                headers={"Content-Type": "application/json"},
            )
        except httpx.RequestError as e:
            await _record_cost_event_safe(
                job_id=job_id,
                stage="tts",
                process="generate_tts_audio",
                provider="runpod_tts",
                attempt_no=1,
                status="failed",
                started_at=request_started_at,
                ended_at=datetime.now(timezone.utc),
                usage_json={"seed": seed, "script_chars": _script_char_count(script_text)},
                raw_response_json={"endpoint": endpoint},
                cost_usd=estimated_cost,
                pricing_kind=_estimated_pricing_kind(estimated_cost),
                pricing_source=_estimated_pricing_source(estimated_cost),
                api_key_family="tts_generation",
                error_type="tts_network_error",
                error_message=f"{type(e).__name__}: {e}",
                idempotency_key=f"tts:{job_id}:{seed}:network:{call_nonce}",
            )
            raise RuntimeError(f"TTS API failed [tts_network_error]: {type(e).__name__}: {e}") from e
        if not resp.is_success:
            await _record_cost_event_safe(
                job_id=job_id,
                stage="tts",
                process="generate_tts_audio",
                provider="runpod_tts",
                attempt_no=1,
                status="failed",
                started_at=request_started_at,
                ended_at=datetime.now(timezone.utc),
                usage_json={"seed": seed, "script_chars": _script_char_count(script_text)},
                raw_response_json={"endpoint": endpoint, "status_code": resp.status_code, "body": (resp.text or "")[:500]},
                cost_usd=estimated_cost,
                pricing_kind=_estimated_pricing_kind(estimated_cost),
                pricing_source=_estimated_pricing_source(estimated_cost),
                api_key_family="tts_generation",
                error_type=_classify_tts_error(resp.text or "", status_code=resp.status_code),
                error_message=(resp.text or "")[:500],
                idempotency_key=f"tts:{job_id}:{seed}:http:{resp.status_code}:{call_nonce}",
            )
            raise RuntimeError(_format_tts_api_error(resp.status_code, resp.text))
        # 성공: 실제 duration_ms 기반 GPU 비용 계산 (GPU 단가 설정 시 우선 사용)
        success_ended_at = datetime.now(timezone.utc)
        actual_duration_ms = (success_ended_at - request_started_at).total_seconds() * 1000
        gpu_cost = _estimate_tts_cost_usd_from_duration(actual_duration_ms)
        if gpu_cost is not None:
            tts_cost_usd = gpu_cost
            tts_pricing_kind = "estimated"
            tts_pricing_source = "gpu_duration_estimate"
        else:
            tts_cost_usd = estimated_cost
            tts_pricing_kind = _estimated_pricing_kind(estimated_cost)
            tts_pricing_source = _estimated_pricing_source(estimated_cost)
        await _record_cost_event_safe(
            job_id=job_id,
            stage="tts",
            process="generate_tts_audio",
            provider="runpod_tts",
            attempt_no=1,
            status="success",
            started_at=request_started_at,
            ended_at=success_ended_at,
            usage_json={
                "seed": seed,
                "script_chars": _script_char_count(script_text),
                "audio_bytes": len(resp.content or b""),
                "duration_ms": round(actual_duration_ms),
                "gpu_rate_usd_per_hour": float(settings.runpod_gpu_cost_usd_per_hour),
            },
            raw_response_json={"endpoint": endpoint, "status_code": resp.status_code},
            cost_usd=tts_cost_usd,
            pricing_kind=tts_pricing_kind,
            pricing_source=tts_pricing_source,
            api_key_family="tts_generation",
            error_type="",
            error_message="",
            idempotency_key=f"tts:{job_id}:{seed}:success:{call_nonce}",
        )
        return resp.content


async def _run_tts_generation(
    *,
    job_id: str,
    script_text: str,
    channel_id: str,
    user_id: str,
    downstream_intent: str,
    force_random_seeds: bool = False,
) -> None:
    variant_failures: list[dict] = []
    try:
        # 1) 같은 대본으로 TTS 후보 3개를 순차 생성 2) Discord/S3 저장 3) 사용자가 1개를 선택한 뒤 승인 단계로 이동한다.
        await job_service.transition_status(job_id, "GENERATING")
        body_script_text, removed_opening, removed_ending = _strip_fixed_intro_outro_lines(script_text)
        if not body_script_text:
            raise RuntimeError("tts body script is empty after removing fixed opening/ending lines")
        current_job = await job_service.get_job(job_id)
        cleaned_script = _clear_tts_variant_metadata(current_job.get("script_json") if current_job else {})
        await job_service.update_job(
            job_id,
            audio_url="",
            final_url="",
            error_message="",
            script_json=_merge_script_json_with_media_names(
                cleaned_script,
                audio_filename="",
                tts_error_type="",
                tts_error_detail="",
                tts_script_text=body_script_text,
            ),
        )
        batch_id = _next_tts_batch_id()
        seeds, seed_strategy = _resolve_tts_variant_seeds(channel_id, force_random=force_random_seeds)
        logger.info(
            "[tts-seed] resolved channel_id=%s strategy=%s seeds=%s removed_opening=%s removed_ending=%s",
            channel_id,
            seed_strategy,
            ",".join(str(seed) for seed in seeds),
            removed_opening,
            removed_ending,
        )
        variants: list[dict] = []
        success_count = 0
        failure_count = 0
        for variant_index, seed in enumerate(seeds):
            try:
                body_audio_bytes = await _generate_tts_audio_content(job_id, body_script_text, seed=seed)
                (
                    audio_bytes,
                    concat_attempts,
                    body_audio_spec,
                    final_audio_spec,
                    tts_timing,
                ) = await _concat_tts_audio_with_fixed_clips(body_audio_bytes)
                variant_info = await _store_and_send_tts_variant(
                    job_id=job_id,
                    channel_id=channel_id,
                    batch_id=batch_id,
                    variant_index=variant_index,
                    seed=seed,
                    concat_attempts=concat_attempts,
                    audio_bytes=audio_bytes,
                    tts_timing=tts_timing,
                )
                variant_info["seed"] = seed
                variant_info["tts_body_audio_bytes"] = len(body_audio_bytes)
                variant_info["tts_final_audio_bytes"] = len(audio_bytes)
                variant_info["tts_body_audio_spec"] = body_audio_spec
                variant_info["tts_final_audio_spec"] = final_audio_spec
                variants.append(variant_info)
                success_count += 1
            except Exception as e:
                failure_entry = _build_tts_variant_failure_entry(
                    variant_index=variant_index,
                    seed=seed,
                    error=e,
                )
                variant_failures.append(failure_entry)
                logger.exception(
                    "[tts-direct] variant failed job_id=%s batch_id=%s variant=%s seed=%s type=%s status=%s",
                    job_id,
                    batch_id,
                    variant_index,
                    seed,
                    failure_entry["error_type"],
                    failure_entry["status_code"],
                )
                variants.append(
                    {
                        "variant_index": variant_index,
                        "seed": seed,
                        "filename": _build_tts_variant_filename(job_id, variant_index),
                        "s3_uri": "",
                        "attachment_or_presigned_url": "",
                        "discord_message_id": "",
                        "status": "failed",
                        "error_type": failure_entry["error_type"],
                        "status_code": failure_entry["status_code"],
                        "error": failure_entry["error_snippet"],
                    }
                )
                failure_count += 1

        if success_count == 0:
            failure_summary = _build_tts_failure_summary(variant_failures)
            if failure_summary:
                raise RuntimeError(f"TTS 후보 3개 생성이 모두 실패했습니다. {failure_summary}")
            raise RuntimeError("TTS 후보 3개 생성이 모두 실패했습니다.")

        control_message_id = await _discord_adapter.send_tts_variant_control_message(
            channel_id=channel_id,
            job_id=job_id,
            batch_id=batch_id,
            caption=_build_tts_variant_control_caption(
                success_count=success_count,
                failure_count=failure_count,
                downstream_intent=downstream_intent,
                seed_strategy=seed_strategy,
                seeds=seeds,
            ),
        )

        latest_job = await job_service.get_job(job_id)
        merged_script = _merge_script_json_with_media_names(
            _clear_tts_variant_metadata(latest_job.get("script_json") if latest_job else {}),
            tts_error_type="",
            tts_error_detail="",
        )
        merged_script["tts_variants"] = variants
        merged_script["active_tts_batch_id"] = batch_id
        merged_script["selected_tts_variant_index"] = None
        merged_script["tts_variant_control_message_id"] = control_message_id
        merged_script["tts_variant_action_message_id"] = ""
        merged_script["tts_downstream_intent"] = downstream_intent
        merged_script["tts_seed_strategy"] = seed_strategy
        merged_script["tts_seed_values"] = [int(seed) for seed in seeds]
        merged_script["selected_tts_seed"] = None
        merged_script["selected_tts_timing"] = {}
        merged_script["tts_body_script_text"] = body_script_text
        merged_script["tts_fixed_clips_enabled"] = True
        merged_script["tts_fixed_clips_removed_opening"] = removed_opening
        merged_script["tts_fixed_clips_removed_ending"] = removed_ending
        if variant_failures:
            merged_script["tts_variant_failures"] = variant_failures
            merged_script["tts_failure_summary"] = _build_tts_failure_summary(variant_failures)
            merged_script["tts_last_error_at"] = datetime.now(timezone.utc).isoformat()
        await job_service.update_job(
            job_id,
            audio_url="",
            final_url="",
            error_message="",
            script_json=merged_script,
        )
        await job_service.transition_status(job_id, "APPROVED")
    except Exception as e:
        logger.exception("[tts-direct] failed job_id=%s: %r", job_id, e)
        error_text = str(e)
        error_type = _extract_tts_error_type(error_text)
        failure_summary = _build_tts_failure_summary(variant_failures)
        await job_service.transition_status(job_id, "FAILED")
        current_job = await job_service.get_job(job_id)
        failed_script = _merge_script_json_with_media_names(
            current_job.get("script_json") if current_job else None,
            tts_error_type=error_type,
            tts_error_detail=error_text,
        )
        if variant_failures:
            failed_script["tts_variant_failures"] = variant_failures
            failed_script["tts_failure_summary"] = failure_summary
            failed_script["tts_last_error_at"] = datetime.now(timezone.utc).isoformat()
        await job_service.update_job(
            job_id,
            error_message=f"TTS 생성 실패 [{error_type}]: {error_text}",
            script_json=failed_script,
        )
        notify_reason = failure_summary or _clip(error_text, limit=260)
        try:
            await _discord_adapter.send_text_message(
                channel_id,
                f"❌ TTS 생성에 실패했습니다.\nJob ID: {job_id[:8]}\n오류 유형: {error_type}\n사유: {notify_reason}",
            )
        except Exception as notify_err:
            logger.error("[tts-direct] failure notify failed job_id=%s: %s", job_id, notify_err)


def _spawn_tts_generation(
    *,
    job_id: str,
    script_text: str,
    channel_id: str,
    user_id: str,
    downstream_intent: str,
    force_random_seeds: bool = False,
) -> None:
    # slash command/버튼 응답을 오래 붙잡지 않기 위해
    # 실제 TTS 생성은 background task로 분리한다.
    _launch_bg_task(
        _run_tts_generation(
            job_id=job_id,
            script_text=script_text,
            channel_id=channel_id,
            user_id=user_id,
            downstream_intent=downstream_intent,
            force_random_seeds=force_random_seeds,
        ),
        task_name="tts-generate",
        job_id=job_id,
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
        latest = await job_service.get_latest_job_in_channel(
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
        if exact.get("messenger_channel_id") != channel_id:
            raise HTTPException(status_code=403, detail="Job belongs to a different channel")
        return exact

    matches = await job_service.find_jobs_by_prefix_in_channel(
        normalized_job_id,
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


def _coerce_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            return None
    return None


def _is_auto_report_job_stale(job: dict, stale_minutes: int) -> bool:
    updated_at = _coerce_datetime(job.get("updated_at")) or _coerce_datetime(job.get("created_at"))
    if updated_at is None:
        return False
    return updated_at < (datetime.now(timezone.utc) - timedelta(minutes=max(1, stale_minutes)))


def _is_auto_report_job(job: dict | None) -> bool:
    if not job:
        return False
    return (job.get("messenger_user_id") or "").strip() == "system:auto-report"


def _should_skip_auto_report_discord_delivery(job: dict | None) -> bool:
    # 시간별 자동 보고서는 S3/DB 적재까지만 두고,
    # Discord 노출은 설정으로 별도 제어한다.
    if settings.auto_report_discord_delivery_enabled:
        return False
    return _is_auto_report_job(job)


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


def _validate_request_contracts() -> None:
    # 런타임 모델/핸들러 스키마가 어긋나면 시작 시점에 명시적으로 로그를 남긴다.
    required_fields = {"job_id", "action", "targets", "publish_title"}
    model_fields = set(getattr(VideoActionRequest, "model_fields", {}).keys())
    missing = sorted(required_fields - model_fields)
    if missing:
        logger.error(
            "[startup] VideoActionRequest contract mismatch missing_fields=%s current_fields=%s",
            ",".join(missing),
            ",".join(sorted(model_fields)),
        )
    else:
        logger.info("[startup] VideoActionRequest contract OK fields=%s", ",".join(sorted(model_fields)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _discord_adapter

    _validate_request_contracts()
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


def _verify_cost_viewer_auth(credentials: Annotated[HTTPBasicCredentials, Depends(_http_basic)]) -> None:
    configured_user = (settings.cost_viewer_basic_user or "").strip()
    configured_password = (settings.cost_viewer_basic_password or "").strip()
    if not configured_user or not configured_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cost viewer credentials are not configured",
        )
    user_ok = secrets.compare_digest(credentials.username, configured_user)
    password_ok = secrets.compare_digest(credentials.password, configured_password)
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


CostViewerAuthDep = Annotated[None, Depends(_verify_cost_viewer_auth)]


# ─────────────────────────────────────────
# 어댑터 반환 헬퍼
# ─────────────────────────────────────────

_SEEDLAB_DEFAULT_SAMPLES = 30
_SEEDLAB_DUP_SAMPLES = 10
_SEEDLAB_DEFAULT_TAKES = 1
_SEEDLAB_DUP_TAKES = 3
_SEEDLAB_SAMPLE_CONCURRENCY = 2


def _seedlab_signing_secret_bytes() -> bytes:
    raw = (settings.seedlab_signing_secret or "").strip() or (settings.gateway_internal_secret or "").strip()
    if not raw:
        raise HTTPException(status_code=503, detail="Seed Lab signing secret is not configured")
    return raw.encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * ((4 - (len(raw) % 4)) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _build_seedlab_signed_token(*, run_id: str, user_id: str, expires_at: datetime) -> str:
    payload = {
        "run_id": run_id,
        "discord_user_id": user_id,
        "exp": int(expires_at.timestamp()),
    }
    payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_seedlab_signing_secret_bytes(), payload_raw, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_raw)}.{_b64url_encode(sig)}"


def _verify_seedlab_signed_token(token: str) -> dict[str, Any]:
    try:
        payload_part, sig_part = token.split(".", 1)
    except ValueError as e:
        raise HTTPException(status_code=403, detail="Invalid seedlab token") from e
    payload_raw = _b64url_decode(payload_part)
    actual_sig = _b64url_decode(sig_part)
    expected_sig = hmac.new(_seedlab_signing_secret_bytes(), payload_raw, hashlib.sha256).digest()
    if not secrets.compare_digest(actual_sig, expected_sig):
        raise HTTPException(status_code=403, detail="Invalid seedlab token")
    payload = json.loads(payload_raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise HTTPException(status_code=403, detail="Invalid seedlab token payload")
    expires_at = int(payload.get("exp") or 0)
    if expires_at <= int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=403, detail="Seedlab link expired")
    run_id = str(payload.get("run_id") or "").strip()
    user_id = str(payload.get("discord_user_id") or "").strip()
    if not run_id or not user_id:
        raise HTTPException(status_code=403, detail="Invalid seedlab token payload")
    return payload


async def _seedlab_service_request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    content: bytes | None = None,
    headers: Optional[dict[str, str]] = None,
) -> httpx.Response:
    service_base_url = (settings.seedlab_service_url or "").strip().rstrip("/")
    if not service_base_url:
        raise HTTPException(status_code=503, detail="Seed Lab service URL is not configured")
    request_headers = {"X-Internal-Secret": settings.gateway_internal_secret}
    if headers:
        request_headers.update(headers)
    try:
        response = await _http_client.request(
            method,
            f"{service_base_url}{path}",
            json=json_body,
            content=content,
            headers=request_headers,
            timeout=120.0,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Seed Lab service unavailable: {e}") from e
    return response


def _build_seedlab_link_from_run(run: dict[str, Any]) -> str:
    user_id = str(run.get("discord_user_id") or "").strip()
    run_id = str(run.get("run_id") or "").strip()
    expires_at = run.get("signed_link_expires_at")
    if not user_id or not run_id or expires_at is None:
        return ""
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except Exception:
            return ""
    if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if not isinstance(expires_at, datetime):
        return ""
    token = _build_seedlab_signed_token(run_id=run_id, user_id=user_id, expires_at=expires_at)
    base_url = (settings.seedlab_public_base_url or "").strip().rstrip("/")
    return f"{base_url}/seedlab/r/{token}/" if base_url else f"/seedlab/r/{token}/"


def _cost_viewer_signing_secret_bytes() -> bytes:
    raw = (settings.seedlab_signing_secret or "").strip() or (settings.gateway_internal_secret or "").strip()
    if not raw:
        raise HTTPException(status_code=503, detail="Cost viewer signing secret is not configured")
    return raw.encode("utf-8")


def _build_cost_viewer_signed_token(*, user_id: str, expires_at: datetime) -> str:
    payload = {
        "discord_user_id": user_id,
        "exp": int(expires_at.timestamp()),
    }
    payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_cost_viewer_signing_secret_bytes(), payload_raw, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_raw)}.{_b64url_encode(sig)}"


def _verify_cost_viewer_signed_token(token: str) -> dict[str, Any]:
    try:
        payload_part, sig_part = token.split(".", 1)
    except ValueError as e:
        raise HTTPException(status_code=403, detail="Invalid cost viewer token") from e
    payload_raw = _b64url_decode(payload_part)
    actual_sig = _b64url_decode(sig_part)
    expected_sig = hmac.new(_cost_viewer_signing_secret_bytes(), payload_raw, hashlib.sha256).digest()
    if not secrets.compare_digest(actual_sig, expected_sig):
        raise HTTPException(status_code=403, detail="Invalid cost viewer token")
    payload = json.loads(payload_raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise HTTPException(status_code=403, detail="Invalid cost viewer token payload")
    expires_at = int(payload.get("exp") or 0)
    if expires_at <= int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=403, detail="Cost viewer link expired")
    user_id = str(payload.get("discord_user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=403, detail="Invalid cost viewer token payload")
    return payload


def _resolve_cost_viewer_base_url() -> str:
    base_url = (settings.cost_viewer_public_base_url or "").strip().rstrip("/")
    if base_url:
        return base_url
    fallback = (settings.seedlab_public_base_url or "").strip().rstrip("/")
    if fallback:
        return fallback
    raise HTTPException(status_code=503, detail="Cost viewer public base URL is not configured")


def _build_cost_viewer_link(*, user_id: str, expires_at: datetime) -> str:
    token = _build_cost_viewer_signed_token(user_id=user_id, expires_at=expires_at)
    return f"{_resolve_cost_viewer_base_url()}/cost/r/{token}/"


def _format_seedlab_progress_text(run: dict[str, Any], body: SeedLabProgressRequest) -> str:
    run_id = str(run.get("run_id") or body.run_id)
    stage = (body.stage or body.status or "queued").strip().lower()
    eval_location = str(body.eval_location or "").strip().lower()
    total_count = max(0, int(body.total_count or 0))
    generated_count = max(0, int(body.generated_count or 0))
    evaluated_count = max(0, int(body.evaluated_count or 0))
    ready_count = max(0, int(body.ready_count or 0))
    failed_count = max(0, int(body.failed_count or 0))
    eval_failed_count = max(0, int(body.eval_failed_count or 0))
    runpod_job_count = max(0, int(body.runpod_job_count or 0))
    gpu_active_sample_count = max(0, int(body.gpu_active_sample_count or 0))
    remote_eval_failed_count = max(0, int(body.remote_eval_failed_count or 0))
    avg_stage_timings = body.avg_stage_timings_ms if isinstance(body.avg_stage_timings_ms, dict) else {}
    executor_counts = body.eval_executor_counts if isinstance(body.eval_executor_counts, dict) else {}
    if stage == "queued":
        stage_label = "대기 중"
    elif stage == "generating":
        stage_label = "샘플 생성 중"
    elif stage == "auto_evaluating":
        stage_label = "AI 평가 중"
    elif stage == "ready":
        stage_label = "완료"
    elif stage == "failed":
        stage_label = "실패"
    else:
        stage_label = stage or "대기 중"

    if stage == "auto_evaluating":
        ai_total = ready_count or max(generated_count - failed_count, 0)
        ai_line = f"AI 평가: {evaluated_count}/{ai_total}"
    elif stage == "ready":
        ai_total = ready_count or max(generated_count - failed_count, 0)
        ai_line = f"AI 평가: {evaluated_count}/{ai_total}" if evaluated_count > 0 else "AI 평가: 비활성"
    elif stage == "failed":
        ai_total = ready_count or max(generated_count - failed_count, 0)
        ai_line = f"AI 평가: {evaluated_count}/{ai_total}" if (evaluated_count > 0 or ai_total > 0) else "AI 평가: 진행 전 중단"
    else:
        ai_line = "AI 평가: 대기 중"
    if eval_location:
        ai_line = f"{ai_line} ({eval_location})"

    lines = [
        "🧪 Seed Lab 실행 중",
        f"Run ID: `{run_id}`",
        f"상태: {stage_label}",
        f"생성: {generated_count}/{total_count}",
        f"실패: {failed_count}",
        ai_line,
    ]
    if evaluated_count > 0 or runpod_job_count > 0 or remote_eval_failed_count > 0:
        lines.append(
            "실행: "
            f"RunPod {runpod_job_count} / GPU {gpu_active_sample_count} / "
            f"AI 실패 {eval_failed_count} / 원격 실패 {remote_eval_failed_count}"
        )
    if executor_counts:
        executor_chunks = [f"{str(key)}={int(value or 0)}" for key, value in executor_counts.items()]
        if executor_chunks:
            lines.append(f"평가자: {', '.join(executor_chunks)}")
    if avg_stage_timings:
        timing_chunks = []
        for key in ("reference_load", "asr", "signal_analysis", "judge_note", "total"):
            value = avg_stage_timings.get(key)
            if isinstance(value, (int, float)):
                timing_chunks.append(f"{key}={round(float(value), 1)}ms")
        if timing_chunks:
            lines.append(f"평균 지연: {', '.join(timing_chunks)}")
    link = _build_seedlab_link_from_run(run)
    if link:
        lines.append(f"링크: {link}")
    if body.remote_eval_last_error:
        lines.append(f"원격 평가 오류: {_clip_text(str(body.remote_eval_last_error), 220)}")
    if body.last_error:
        lines.append(f"사유: {_clip_text(str(body.last_error), 250)}")
    return "\n".join(lines)


async def _ensure_seedlab_progress_message(run: dict[str, Any], text: str) -> str:
    channel_id = str(run.get("discord_channel_id") or "").strip()
    if not channel_id:
        raise RuntimeError("seedlab discord channel id is missing")
    existing_message_id = str(run.get("progress_message_id") or "").strip()
    if existing_message_id:
        try:
            await _discord_adapter.edit_seedlab_progress_message(channel_id, existing_message_id, text)
            return existing_message_id
        except Exception as e:
            logger.warning(
                "[discord] seedlab progress edit failed run=%s message_id=%s err=%s",
                run.get("run_id"),
                existing_message_id,
                e,
            )
    message_id = await _discord_adapter.send_seedlab_progress_message(channel_id, text)
    await job_service.update_seed_lab_run(run["run_id"], progress_message_id=message_id)
    return message_id

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

    if job["status"] in ("APPROVED", "REPORT_READY", "PUBLISHED", "PUBLISHING"):
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

    source_url = (body.source_url or str(notebook_state.get("latest_source_url") or "")).strip()
    source_title = (body.source_title or str(notebook_state.get("latest_source_title") or "")).strip()
    if not source_url:
        logger.info(
            "[auto-report] skip channel=%s notebook=%s reason=missing-latest-source",
            body.channel_id,
            notebook_url,
        )
        return {
            "status": "skipped",
            "reason": "missing-latest-source",
            "notebook_url": notebook_url,
        }

    stale_minutes = max(1, int(settings.auto_report_stale_minutes))
    max_attempts_per_day = max(1, int(settings.auto_report_max_attempts_per_day))
    in_progress_statuses = {
        "DRAFT",
        "SCRIPTING",
        "GENERATING",
        "WAITING_APPROVAL",
        "REVISION_REQUESTED",
        "APPROVED",
        "REPORT_READY",
        "WAITING_VIDEO_APPROVAL",
        "PUBLISHING",
    }

    latest_auto_job = await job_service.get_latest_auto_report_job(
        channel_id=body.channel_id,
        notebook_url=notebook_url,
    )
    if latest_auto_job is not None:
        latest_status = str(latest_auto_job.get("status") or "").strip().upper()
        if latest_status in in_progress_statuses:
            if _is_auto_report_job_stale(latest_auto_job, stale_minutes):
                stale_reason = (
                    f"auto-report stale timeout: {stale_minutes}m "
                    f"(prev_job={latest_auto_job['id']}, prev_status={latest_status})"
                )
                await job_service.transition_status(latest_auto_job["id"], "FAILED")
                await job_service.update_job(latest_auto_job["id"], error_message=stale_reason)
                logger.warning(
                    "[auto-report] stale job failed channel=%s notebook=%s prev_job=%s prev_status=%s",
                    body.channel_id,
                    notebook_url,
                    latest_auto_job["id"],
                    latest_status,
                )
            else:
                logger.info(
                    "[auto-report] skip channel=%s notebook=%s reason=in-progress job_id=%s status=%s",
                    body.channel_id,
                    notebook_url,
                    latest_auto_job["id"],
                    latest_status,
                )
                return {
                    "status": "skipped",
                    "reason": "in-progress",
                    "job_id": latest_auto_job["id"],
                    "job_status": latest_status,
                    "notebook_url": notebook_url,
                }
        elif latest_status == "PUBLISHED":
            logger.info(
                "[auto-report] skip channel=%s notebook=%s reason=already-published job_id=%s",
                body.channel_id,
                notebook_url,
                latest_auto_job["id"],
            )
            return {
                "status": "skipped",
                "reason": "already-published",
                "job_id": latest_auto_job["id"],
                "job_status": latest_status,
                "notebook_url": notebook_url,
            }

    attempts_today = await job_service.count_auto_report_attempts_today(
        channel_id=body.channel_id,
        notebook_url=notebook_url,
    )
    if attempts_today >= max_attempts_per_day:
        logger.info(
            "[auto-report] skip channel=%s notebook=%s reason=daily-limit attempts=%s/%s",
            body.channel_id,
            notebook_url,
            attempts_today,
            max_attempts_per_day,
        )
        return {
            "status": "skipped",
            "reason": "daily-limit",
            "attempts_today": attempts_today,
            "max_attempts_per_day": max_attempts_per_day,
            "notebook_url": notebook_url,
        }

    target_channel_id = _get_primary_discord_channel_id()
    prompt = _build_auto_report_prompt(
        AutoReportRequest(
            channel_id=body.channel_id,
            channel_name=body.channel_name,
            source_url=source_url,
            source_title=source_title,
        )
    )
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
            "auto_report_source_url": source_url,
            "auto_report_source_title": source_title,
        },
    )
    try:
        wf06_ok = await _call_wf06(report_body, raise_on_error=True)
        if not wf06_ok:
            raise HTTPException(status_code=500, detail="WF-06 trigger failed")
    except Exception as e:
        await job_service.transition_status(job_id, "FAILED")
        await job_service.update_job(job_id, error_message=f"WF-06 trigger failed: {e}")
        raise
    logger.info(
        "[auto-report] triggered job_id=%s discord_channel=%s youtube_channel=%s source=%s",
        job_id,
        target_channel_id,
        body.channel_id,
        source_url,
    )
    return {
        "status": "triggered",
        "job_id": job_id,
        "messenger_channel_id": target_channel_id,
        "youtube_channel_id": body.channel_id,
        "attempts_today": attempts_today + 1,
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
        stored, upload_error, is_link_only_report = _upload_subtitle_report_file(
            job_id=body.job_id,
            filename=filename,
            file_bytes=file_bytes,
        )
        if upload_error is not None:
            error_message = f"subtitle report upload failed: {upload_error}"
            logger.error("[report-select] %s job_id=%s", error_message, body.job_id)
            await job_service.transition_status(body.job_id, "FAILED")
            await job_service.update_job(
                body.job_id,
                error_message=error_message,
                script_json=merged_script,
                final_url=None,
            )
            await _discord_adapter.send_text_message(channel_id, "❌ 자막 파일 S3 저장에 실패했습니다. 다시 시도해주세요.")
            return

        report_storage_url = stored.s3_uri if stored else ""
        await job_service.update_job(
            body.job_id,
            script_json=merged_script,
            final_url=report_storage_url or None,
        )

        text = final_script_text
        if len(text) > 1800:
            overflow_hint = (
                "[전체 내용은 아래 링크 참조]"
                if is_link_only_report
                else "[전체 내용은 첨부 파일 참조]"
            )
            text = text[:1800] + f"\n\n{overflow_hint}"

        try:
            if is_link_only_report:
                await _discord_adapter.send_report_link_message(
                    channel_id=channel_id,
                    text=text,
                    report_url=stored.presigned_url if stored else "",
                    include_tts_button=True,
                    include_video_button=True,
                    job_id=body.job_id,
                )
            else:
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

        await job_service.transition_status(body.job_id, "REPORT_READY")
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
    manual_report_retry = existing_job is not None and not _is_auto_report_job(existing_job)
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

    stored, upload_error, is_link_only_report = _upload_subtitle_report_file(
        job_id=body.job_id,
        filename=filename,
        file_bytes=file_bytes,
    )
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

    is_auto_report_job = _is_auto_report_job(existing_job)
    should_skip_discord_delivery = _should_skip_auto_report_discord_delivery(existing_job)
    logger.info(
        "[discord] send_report decision job_id=%s auto_report=%s delivery_enabled=%s decision=%s",
        body.job_id,
        is_auto_report_job,
        settings.auto_report_discord_delivery_enabled,
        "stored_only" if should_skip_discord_delivery else "sent",
    )

    if should_skip_discord_delivery:
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
    if isinstance(body.cost_event, dict) and body.cost_event:
        payload = dict(body.cost_event)
        payload["job_id"] = str(payload.get("job_id") or body.job_id or "").strip()
        if payload["job_id"]:
            try:
                await cost_service.ingest_event(payload)
            except Exception as e:
                logger.warning("[cost] failed to ingest cost_event from send-text job_id=%s err=%s", payload["job_id"], e)
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
    existing_script_json = _as_script_json(existing_job.get("script_json") if existing_job else None)
    selected_avatar_index = _get_job_avatar_index(existing_script_json)
    avatar_options: list[dict[str, object]] = []
    if body.include_wf12_button:
        avatar_options = _parse_heygen_avatar_options_from_env()
    filename = _normalize_audio_filename(
        body.job_id,
        body.filename,
        existing_script_json,
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
                selected_avatar_index=selected_avatar_index,
                avatar_options=avatar_options,
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
                selected_avatar_index=selected_avatar_index,
                avatar_options=avatar_options,
            )
        except Exception as e:
            logger.error("[discord] send_tts_audio_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    resolved_audio_url = stored.s3_uri if stored else attachment_url
    merged_script = _merge_script_json_with_media_names(
        existing_script_json,
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
    script_json = _as_script_json(job.get("script_json"))

    if body.action == "select_variant":
        active_batch_id = str(script_json.get("active_tts_batch_id") or "").strip()
        if not body.batch_id:
            raise HTTPException(status_code=400, detail="batch_id is required")
        if active_batch_id != body.batch_id:
            raise HTTPException(status_code=409, detail="stale tts batch")
        if body.variant_index is None or body.variant_index < 0:
            raise HTTPException(status_code=400, detail="variant_index is required")
        if script_json.get("selected_tts_variant_index") is not None:
            raise HTTPException(status_code=409, detail="TTS variant already selected")

        chosen_variant = None
        variants = _get_tts_variants(script_json)
        for variant in variants:
            if int(variant.get("variant_index", -1)) == int(body.variant_index):
                chosen_variant = variant
                break
        if chosen_variant is None:
            raise HTTPException(status_code=404, detail="TTS variant not found")
        if str(chosen_variant.get("status") or "").strip() != "ready":
            raise HTTPException(status_code=409, detail="TTS variant is not selectable")

        resolved_audio_url = str(chosen_variant.get("s3_uri") or chosen_variant.get("attachment_or_presigned_url") or "").strip()
        if not resolved_audio_url:
            raise HTTPException(status_code=400, detail="Selected TTS variant has no audio URL")
        selected_seed_raw = chosen_variant.get("seed")
        selected_seed: Optional[int] = None
        if selected_seed_raw is not None and str(selected_seed_raw).strip():
            try:
                selected_seed = int(str(selected_seed_raw).strip())
            except Exception:
                selected_seed = None

        selected_script = _merge_script_json_with_media_names(
            script_json,
            audio_filename=str(chosen_variant.get("filename") or "").strip(),
            tts_error_type="",
            tts_error_detail="",
        )
        selected_script["tts_variants"] = variants
        selected_script["active_tts_batch_id"] = active_batch_id
        selected_script["selected_tts_variant_index"] = int(body.variant_index)
        selected_script["selected_tts_seed"] = selected_seed
        selected_script["selected_tts_timing"] = dict(chosen_variant.get("tts_timing") or {})
        selected_script["tts_variant_control_message_id"] = str(script_json.get("tts_variant_control_message_id") or "").strip()
        selected_script["tts_variant_action_message_id"] = ""
        selected_script["tts_downstream_intent"] = _get_tts_downstream_intent(script_json)
        selected_script["tts_seed_strategy"] = str(script_json.get("tts_seed_strategy") or "").strip()
        selected_script["tts_seed_values"] = list(script_json.get("tts_seed_values") or [])
        await job_service.update_job(
            body.job_id,
            audio_url=resolved_audio_url,
            final_url=str(chosen_variant.get("s3_uri") or chosen_variant.get("attachment_or_presigned_url") or "").strip(),
            script_json=selected_script,
        )
        try:
            avatar_options = _parse_heygen_avatar_options_from_env()
            selected_avatar_label = _get_job_avatar_label(script_json)
            approval_message_id = await _discord_adapter.send_tts_approval_message(
                channel_id=channel_id,
                job_id=body.job_id,
                caption=_build_selected_tts_caption(
                    downstream_intent=_get_tts_downstream_intent(script_json),
                    variant_index=int(body.variant_index),
                    selected_seed=selected_seed,
                    selected_avatar_label=selected_avatar_label,
                ),
                selected_avatar_index=_get_job_avatar_index(script_json),
                avatar_options=avatar_options,
            )
        except Exception:
            rollback_script = _merge_script_json_with_media_names(_clear_tts_variant_metadata(script_json), tts_error_type="", tts_error_detail="")
            rollback_script["tts_variants"] = variants
            rollback_script["active_tts_batch_id"] = active_batch_id
            rollback_script["selected_tts_variant_index"] = None
            rollback_script["selected_tts_seed"] = None
            rollback_script["selected_tts_timing"] = {}
            rollback_script["tts_variant_control_message_id"] = str(script_json.get("tts_variant_control_message_id") or "").strip()
            rollback_script["tts_variant_action_message_id"] = ""
            rollback_script["tts_downstream_intent"] = _get_tts_downstream_intent(script_json)
            rollback_script["tts_seed_strategy"] = str(script_json.get("tts_seed_strategy") or "").strip()
            rollback_script["tts_seed_values"] = list(script_json.get("tts_seed_values") or [])
            await job_service.update_job(body.job_id, audio_url="", final_url="", script_json=rollback_script)
            raise
        selected_script["tts_variant_action_message_id"] = approval_message_id
        await job_service.update_job(body.job_id, script_json=selected_script)
        await _disable_tts_variant_buttons(channel_id, script_json)
        logger.info("[discord] tts_action=select_variant job_id=%s batch_id=%s variant=%s", body.job_id, active_batch_id, body.variant_index)
        return {
            "job_id": body.job_id,
            "action": body.action,
            "batch_id": active_batch_id,
            "variant_index": int(body.variant_index),
            "selected_seed": selected_seed,
            "audio_url": resolved_audio_url,
        }

    if body.action == "regenerate_batch":
        active_batch_id = str(script_json.get("active_tts_batch_id") or "").strip()
        if not body.batch_id:
            raise HTTPException(status_code=400, detail="batch_id is required")
        if active_batch_id != body.batch_id:
            raise HTTPException(status_code=409, detail="stale tts batch")
        script_text = _get_tts_script_text(script_json)
        if not script_text:
            raise HTTPException(status_code=400, detail="No script found in job")
        await _disable_tts_variant_buttons(channel_id, script_json)
        await job_service.update_job(
            body.job_id,
            audio_url="",
            final_url="",
            error_message="",
            script_json=_merge_script_json_with_media_names(_clear_tts_variant_metadata(script_json), tts_error_type="", tts_error_detail=""),
        )
        _spawn_tts_generation(
            job_id=body.job_id,
            script_text=script_text,
            channel_id=channel_id,
            user_id=user_id,
            downstream_intent=_get_tts_downstream_intent(script_json),
            force_random_seeds=True,
        )
        await _discord_adapter.send_text_message(channel_id, "🔁 랜덤 seed로 TTS 후보 3개를 다시 생성합니다...")
        logger.info("[discord] tts_action=regenerate_batch job_id=%s batch_id=%s", body.job_id, active_batch_id)
        return {"job_id": body.job_id, "action": body.action, "batch_id": active_batch_id}

    if body.action == "select_avatar":
        selected_variant_index_raw = script_json.get("selected_tts_variant_index")
        if selected_variant_index_raw is None:
            raise HTTPException(status_code=409, detail="TTS 후보를 먼저 선택하세요.")
        if body.avatar_index is None:
            raise HTTPException(status_code=400, detail="avatar_index is required")
        options = _parse_heygen_avatar_options_from_env()
        avatar_index = int(body.avatar_index)
        if avatar_index < 0 or avatar_index >= len(options):
            raise HTTPException(status_code=400, detail=f"avatar_index must be between 0 and {len(options) - 1}")
        selected = options[avatar_index]
        avatar_id = str(selected["avatar_id"])
        avatar_label = str(selected["label"])

        updated_script = _merge_script_json_with_media_names(
            script_json,
            heygen_avatar_id=avatar_id,
        )
        updated_script["avatar_id"] = avatar_id
        updated_script["heygen_avatar_label"] = avatar_label
        updated_script["heygen_avatar_index"] = avatar_index
        await job_service.update_job(body.job_id, script_json=updated_script)
        logger.info("[discord] tts_action=select_avatar job_id=%s avatar_index=%s label=%s", body.job_id, avatar_index, avatar_label)
        return {
            "job_id": body.job_id,
            "action": body.action,
            "avatar_id": avatar_id,
            "avatar_label": avatar_label,
            "avatar_index": avatar_index,
        }

    if body.action in {"approve_standard", "approve_hd"}:
        # WF-12는 외부 URL을 읽어야 하므로 s3:// 저장값은 presigned URL로 바꿔 넘긴다.
        resolved_avatar_id, selected_avatar_label, selected_avatar_index = _resolve_selected_heygen_avatar(script_json)
        avatar_source = f"selected:{selected_avatar_label}"
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
            script_json,
            heygen_avatar_id=resolved_avatar_id,
            heygen_use_avatar_iv_model=use_avatar_iv_model,
        )
        merged_script["avatar_id"] = resolved_avatar_id
        merged_script["heygen_avatar_label"] = selected_avatar_label
        merged_script["heygen_avatar_index"] = selected_avatar_index
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
                f"🎬 WF-12(HeyGen) 영상 생성을 시작합니다. 모드: {mode_text} / 아바타: {selected_avatar_label}",
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
            "avatar_label": selected_avatar_label,
            "avatar_index": selected_avatar_index,
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
    # WF-12가 준 raw video를 내려받아 raw/hardburn/SRT를 모두 S3에 저장한 뒤,
    # Discord에는 hardburn preview 링크와 승인/반려 버튼을 보낸다.
    existing_job = await job_service.get_job(body.job_id)
    if existing_job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    existing_script_json = _as_script_json(existing_job.get("script_json"))
    subtitle_script_text = _get_subtitle_script_text(existing_script_json)
    tts_script_text = _get_tts_script_text(existing_script_json)
    audio_url = str(existing_job.get("audio_url") or "").strip()
    selected_tts_timing = (
        existing_script_json.get("selected_tts_timing")
        if isinstance(existing_script_json.get("selected_tts_timing"), dict)
        else {}
    )
    normalized_video_filename = _normalize_video_filename(
        body.job_id,
        body.video_filename,
        existing_script_json,
    )
    hardburn_video_filename = _build_hardburn_video_filename(normalized_video_filename)
    srt_filename = _build_srt_filename(normalized_video_filename)
    video_started_at = datetime.now(timezone.utc)
    video_resp: Optional[httpx.Response] = None
    raw_stored = None
    srt_stored = None
    hardburn_stored = None
    caption_artifacts: dict[str, Any] = {}
    hardburn_video_bytes = b""
    message_id = ""
    hardburn_started_at = datetime.now(timezone.utc)
    failed_stage = ""
    try:
        if not subtitle_script_text:
            raise RuntimeError("subtitle_script_text is required for hardburn")
        if not tts_script_text:
            raise RuntimeError("tts_script_text is required for hardburn")
        if not audio_url:
            raise RuntimeError("audio_url is required for hardburn")

        failed_stage = "video_download"
        video_resp = await _http_client.get(body.video_url, timeout=300.0)
        video_resp.raise_for_status()

        failed_stage = "raw_video_upload"
        raw_stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_videos,
            filename=normalized_video_filename,
            content=video_resp.content,
            content_type=video_resp.headers.get("content-type") or "video/mp4",
        )

        failed_stage = "caption_artifacts"
        caption_artifacts = await _build_caption_artifacts(
            job_id=body.job_id,
            audio_url=audio_url,
            subtitle_script_text=subtitle_script_text,
            tts_script_text=tts_script_text,
            selected_tts_timing=selected_tts_timing,
        )

        failed_stage = "srt_upload"
        srt_content = str(caption_artifacts.get("srt_content") or "")
        srt_stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_srt,
            filename=srt_filename,
            content=srt_content.encode("utf-8"),
            content_type="application/x-subrip; charset=utf-8",
        )

        failed_stage = "hardburn_render"
        hardburn_video_bytes = await asyncio.to_thread(
            _render_hardburn_video_sync,
            raw_video_bytes=video_resp.content,
            srt_content=srt_content,
        )

        failed_stage = "hardburn_upload"
        hardburn_stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_videos_with_subtitle,
            filename=hardburn_video_filename,
            content=hardburn_video_bytes,
            content_type="video/mp4",
        )

        failed_stage = "discord_send"
        message_id = await _discord_adapter.send_video_preview(
            channel_id=body.channel_id,
            user_id=body.user_id,
            job_id=body.job_id,
            video_url=hardburn_stored.presigned_url,
        )
    except Exception as e:
        detail = str(e.detail) if isinstance(e, HTTPException) else str(e)
        error_message = f"Video preview failed at {failed_stage or 'unknown'}: {detail}"
        logger.error("[video-preview] failed job_id=%s stage=%s err=%s", body.job_id, failed_stage, detail)
        failed_script = _merge_script_json_with_video_preview_failure(
            existing_script_json,
            raw_video_filename=normalized_video_filename,
            hardburn_video_filename=hardburn_video_filename,
            srt_filename=srt_filename,
            raw_video_s3_uri=raw_stored.s3_uri if raw_stored else "",
            hardburn_video_s3_uri=hardburn_stored.s3_uri if hardburn_stored else "",
            srt_s3_uri=srt_stored.s3_uri if srt_stored else "",
            failed_stage=failed_stage,
            error=error_message,
            caption_artifacts=caption_artifacts,
        )
        try:
            await job_service.update_job(
                body.job_id,
                status="FAILED",
                error_message=error_message,
                script_json=failed_script,
            )
        except Exception as update_err:
            logger.error("[video-preview] failed to persist failure state job_id=%s: %s", body.job_id, update_err)
        return {
            "status": "failed",
            "job_id": body.job_id,
            "failed_stage": failed_stage,
            "error_message": error_message,
            "video_filename": hardburn_video_filename,
            "raw_s3_uri": raw_stored.s3_uri if raw_stored else "",
            "hardburn_s3_uri": hardburn_stored.s3_uri if hardburn_stored else "",
            "srt_s3_uri": srt_stored.s3_uri if srt_stored else "",
            "alignment_status": caption_artifacts.get("alignment_status") or "",
            "fallback_reason": caption_artifacts.get("fallback_reason") or "",
        }

    generated_content_id, generated_content_error = await _register_generated_content(
        job_id=body.job_id,
        script_text=subtitle_script_text,
        content_url=hardburn_stored.s3_uri,
    )

    heygen_usage_json = body.heygen_usage_json if isinstance(body.heygen_usage_json, dict) else {}
    request_snapshot = body.heygen_request_snapshot if isinstance(body.heygen_request_snapshot, dict) else {}
    response_snapshot = body.heygen_response_snapshot if isinstance(body.heygen_response_snapshot, dict) else {}
    heygen_cost_usd, heygen_pricing_kind, heygen_pricing_source = _resolve_heygen_cost(
        body.heygen_cost_usd,
        request_snapshot,
        response_snapshot,
        heygen_usage_json,
    )
    await _record_cost_event_safe(
        job_id=body.job_id,
        topic_text=str(existing_job.get("concept_text") or "") if isinstance(existing_job, dict) else "",
        stage="video",
        process="heygen_generate",
        provider="heygen",
        attempt_no=1,
        status="success",
        started_at=video_started_at,
        ended_at=datetime.now(timezone.utc),
        usage_json={
            **heygen_usage_json,
            "video_id": body.heygen_video_id,
            "avatar_id": body.heygen_avatar_id,
            "use_avatar_iv_model": body.heygen_use_avatar_iv_model,
            "status": body.heygen_status or "completed",
            "raw_video_s3_uri": raw_stored.s3_uri,
            "hardburn_video_s3_uri": hardburn_stored.s3_uri,
        },
        raw_response_json={
            "request_snapshot": request_snapshot,
            "response_snapshot": response_snapshot,
            "video_url": body.video_url,
            "video_filename": normalized_video_filename,
            "raw_video_s3_uri": raw_stored.s3_uri,
            "hardburn_video_s3_uri": hardburn_stored.s3_uri,
            "srt_s3_uri": srt_stored.s3_uri,
        },
        cost_usd=heygen_cost_usd,
        pricing_kind=heygen_pricing_kind,
        pricing_source=heygen_pricing_source,
        api_key_family="heygen",
        error_type="",
        error_message="",
        idempotency_key=f"video:heygen:{body.job_id}:{body.heygen_video_id or normalized_video_filename}",
    )
    await _record_cost_event_safe(
        job_id=body.job_id,
        topic_text=str(existing_job.get("concept_text") or ""),
        stage="video",
        process="hardburn_subtitle",
        provider="ffmpeg",
        attempt_no=1,
        status="success",
        started_at=hardburn_started_at,
        ended_at=datetime.now(timezone.utc),
        usage_json={
            "preview_asset": "hardburn",
            "publish_asset": "hardburn",
            "subtitle_sha256": caption_artifacts.get("subtitle_sha256"),
            "asr_model": caption_artifacts.get("asr_model"),
            "word_count": caption_artifacts.get("word_count"),
            "matched_sentence_count": caption_artifacts.get("matched_sentence_count"),
            "raw_video_bytes": len(video_resp.content),
            "hardburn_video_bytes": len(hardburn_video_bytes),
        },
        raw_response_json={
            "caption_request_json": caption_artifacts.get("request_json") or {},
            "srt_filename": srt_filename,
            "hardburn_video_filename": hardburn_video_filename,
        },
        cost_usd=0.0,
        pricing_kind="estimated",
        pricing_source="provider_usage_estimate",
        api_key_family="hardburn_subtitle",
        error_type="",
        error_message="",
        idempotency_key=f"video:hardburn:{body.job_id}:{hardburn_video_filename}",
    )

    merged_script = _merge_script_json_with_media_names(
        existing_script_json,
        video_filename=hardburn_video_filename,
        generated_content_id=generated_content_id if generated_content_id else None,
        generated_content_error=generated_content_error,
    )
    media_names = merged_script.get("media_names") if isinstance(merged_script.get("media_names"), dict) else {}
    media_names["raw_video_filename"] = normalized_video_filename
    media_names["hardburn_video_filename"] = hardburn_video_filename
    media_names["srt_filename"] = srt_filename
    merged_script["media_names"] = media_names
    merged_script["video_assets"] = {
        "raw_video_s3_uri": raw_stored.s3_uri,
        "hardburn_video_s3_uri": hardburn_stored.s3_uri,
        "srt_s3_uri": srt_stored.s3_uri,
        "preview_asset": "hardburn",
        "publish_asset": "hardburn",
    }
    merged_script["video_hardburn"] = {
        "status": "ready",
        "error": "",
        "raw_video_s3_uri": raw_stored.s3_uri,
        "hardburn_video_s3_uri": hardburn_stored.s3_uri,
        "srt_s3_uri": srt_stored.s3_uri,
        "subtitle_sha256": caption_artifacts.get("subtitle_sha256"),
        "cue_count": caption_artifacts.get("cue_count") or caption_artifacts.get("display_sentence_count"),
        "asr_model": caption_artifacts.get("asr_model"),
        "matched_sentence_count": caption_artifacts.get("matched_sentence_count"),
        "word_count": caption_artifacts.get("word_count"),
        "alignment_status": caption_artifacts.get("alignment_status"),
        "fallback_reason": caption_artifacts.get("fallback_reason") or "",
        "timing_source": caption_artifacts.get("timing_source") or "",
        "request_json": caption_artifacts.get("request_json") or {},
    }
    video_storage_url = hardburn_stored.s3_uri
    await job_service.update_job(
        body.job_id,
        confirm_message_id=message_id,
        video_url=video_storage_url,
        final_url=video_storage_url,
        script_json=merged_script,
    )
    try:
        await cost_service.allocate_daily_fixed_cost(target_date=datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).date())
    except Exception as e:
        logger.warning("[cost] daily fixed allocation failed job_id=%s err=%s", body.job_id, e)
    logger.info("[discord] send_video_preview done job_id=%s", body.job_id)
    return {
        "status": "ready",
        "job_id": body.job_id,
        "message_id": message_id,
        "video_filename": hardburn_video_filename,
        "video_url": hardburn_stored.presigned_url,
        "s3_uri": video_storage_url,
        "raw_s3_uri": raw_stored.s3_uri,
        "hardburn_s3_uri": hardburn_stored.s3_uri,
        "srt_s3_uri": srt_stored.s3_uri,
        "preview_asset_type": "hardburn",
    }


@app.post("/internal/wf13-preflight")
async def wf13_preflight(_: AuthDep, body: Wf13PreflightRequest) -> dict:
    channel_ids = _wf13_distinct_channel_ids(body.channel_ids)
    try:
        quota = await _wf13_check_heygen_quota(channel_ids=channel_ids, notify=True)
        return {
            "ok": bool(quota.get("ok")),
            "blocked_reason": str(quota.get("blocked_reason") or ""),
            "remaining_quota": quota.get("remaining_quota"),
            "api_quota": quota.get("api_quota"),
            "notified_channel_ids": list(quota.get("notified_channel_ids") or []),
        }
    except Exception as e:
        logger.warning("[wf13] preflight quota check failed err=%s", e)
        return {
            "ok": True,
            "blocked_reason": "",
            "remaining_quota": None,
            "api_quota": None,
            "notified_channel_ids": [],
            "preflight_error": _clip(str(e), limit=220),
        }


@app.post("/internal/wf13-run-batch")
async def wf13_run_batch(_: AuthDep, body: Wf13RunBatchRequest) -> dict:
    targets = _normalize_publish_targets(body.targets or ["youtube"])
    candidates = await _list_wf13_candidate_jobs()
    selected_job_count = len(candidates)
    if selected_job_count == 0:
        return {
            "status": "skipped",
            "selected_job_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "blocked_count": 0,
            "blocked_reason": "",
            "stopped_on_job_id": "",
            "results": [],
        }

    channel_ids = _wf13_distinct_channel_ids([str(item.get("channel_id") or "") for item in candidates])
    quota = await _wf13_check_heygen_quota(channel_ids=channel_ids, notify=True)
    if not quota.get("ok"):
        return {
            "status": "blocked",
            "selected_job_count": selected_job_count,
            "completed_count": 0,
            "failed_count": 0,
            "blocked_count": selected_job_count,
            "blocked_reason": str(quota.get("blocked_reason") or "heygen_insufficient_credit"),
            "stopped_on_job_id": "",
            "results": [],
        }

    results: list[dict[str, Any]] = []
    completed_count = 0
    failed_count = 0
    blocked_count = 0
    stopped_on_job_id = ""
    final_status = "completed"
    for candidate in candidates:
        req = Wf13RunJobRequest(
            job_id=str(candidate.get("job_id") or ""),
            seed=body.seed,
            avatar_id=body.avatar_id,
            use_avatar_iv_model=body.use_avatar_iv_model,
            targets=targets,
        )
        result = await wf13_run_job(None, req)
        results.append(result)
        status = str(result.get("status") or "")
        if status == "completed":
            completed_count += 1
        elif status == "blocked":
            blocked_count += 1
            final_status = "blocked"
        elif status == "skipped":
            pass
        else:
            failed_count += 1
            if final_status != "blocked":
                final_status = "completed" if completed_count > 0 else "failed"
        if result.get("should_stop_workflow") is True:
            stopped_on_job_id = str(result.get("job_id") or "")
            break

    if stopped_on_job_id and final_status != "blocked":
        final_status = "failed"
    return {
        "status": final_status,
        "selected_job_count": selected_job_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "blocked_count": blocked_count,
        "blocked_reason": (
            str(next((item.get("blocked_reason") for item in results if item.get("blocked_reason")), "")) or ""
        ),
        "stopped_on_job_id": stopped_on_job_id,
        "results": results,
    }


@app.post("/internal/wf13-run-job")
async def wf13_run_job(_: AuthDep, body: Wf13RunJobRequest) -> dict:
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    script_json = _as_script_json(job.get("script_json"))
    channel_id = str(job.get("messenger_channel_id") or "").strip()
    user_id = str(job.get("messenger_user_id") or "").strip()
    targets = _normalize_publish_targets(body.targets or ["youtube"])
    if user_id != "system:auto-report":
        raise HTTPException(status_code=400, detail="WF-13 only supports system:auto-report jobs")

    existing_posts = await job_service.list_platform_posts(body.job_id)
    for post in existing_posts:
        if str(post.get("platform") or "").strip().lower() == "youtube" and _normalized_platform_status(post.get("status")) == "published":
            skipped_script = _merge_script_json_with_wf13_state(
                script_json,
                state="skipped",
                seed=body.seed,
                avatar_id=body.avatar_id,
                use_avatar_iv_model=body.use_avatar_iv_model,
                targets=targets,
                last_completed_stage="publish",
                last_error="",
            )
            await job_service.update_job(body.job_id, script_json=skipped_script)
            return {
                "job_id": body.job_id,
                "status": "skipped",
                "reason": "already_published",
                "failure_stage": "",
                "error_type": "",
                "retryable": False,
                "should_stop_workflow": False,
            }

    try:
        audio_url = str(job.get("audio_url") or "").strip()
        selected_seed = script_json.get("selected_tts_seed")
        audio_ready = bool(audio_url) and str(selected_seed) == str(body.seed)
        video_hardburn = script_json.get("video_hardburn") if isinstance(script_json.get("video_hardburn"), dict) else {}
        video_ready = str(video_hardburn.get("status") or "").strip() == "ready" and bool(str(job.get("video_url") or "").strip())

        if not audio_ready:
            running_tts_script = _merge_script_json_with_wf13_state(
                script_json,
                state="running_tts",
                seed=body.seed,
                avatar_id=body.avatar_id,
                use_avatar_iv_model=body.use_avatar_iv_model,
                targets=targets,
                last_error="",
            )
            await job_service.update_job(body.job_id, script_json=running_tts_script, error_message="")
            await _wf13_notify(channel_id, f"🔊 WF-13 자동 TTS 생성을 시작합니다. seed={body.seed}")
            await _generate_fixed_seed_tts_for_job(
                job_id=body.job_id,
                existing_job=job,
                script_json=script_json,
                seed=body.seed,
                avatar_id=body.avatar_id,
                use_avatar_iv_model=body.use_avatar_iv_model,
                targets=targets,
            )
            job = await job_service.get_job(body.job_id)
            script_json = _as_script_json(job.get("script_json"))
            audio_url = str(job.get("audio_url") or "").strip()

        if not video_ready:
            try:
                quota = await _wf13_check_heygen_quota(channel_ids=[channel_id], notify=False)
                if not quota.get("ok"):
                    raise Wf13PipelineError(
                        f"HeyGen API credit 부족 (remaining_quota={quota.get('remaining_quota')})",
                        error_type="heygen_insufficient_credit",
                        failure_stage="video",
                        retryable=False,
                        should_stop_workflow=True,
                        blocked_reason="heygen_insufficient_credit",
                    )
            except Wf13PipelineError:
                raise
            except Exception as e:
                logger.warning("[wf13] quota precheck failed job_id=%s err=%s", body.job_id, e)
            running_video_script = _merge_script_json_with_wf13_state(
                script_json,
                state="running_video",
                seed=body.seed,
                avatar_id=body.avatar_id,
                use_avatar_iv_model=body.use_avatar_iv_model,
                targets=targets,
                last_error="",
            )
            await job_service.update_job(body.job_id, script_json=running_video_script, error_message="")
            await _wf13_notify(channel_id, "🎬 WF-13 자동 영상 생성을 시작합니다.")
            resolved_audio_url = _resolve_internal_media_url(audio_url)
            heygen_result = await _run_wf13_heygen_generate(
                job_id=body.job_id,
                channel_id=channel_id,
                user_id=user_id,
                audio_url=resolved_audio_url,
                avatar_id=body.avatar_id,
                use_avatar_iv_model=body.use_avatar_iv_model,
            )
            await _finalize_video_auto(
                job_id=body.job_id,
                video_url=str(heygen_result["video_url"]),
                video_filename=str(heygen_result["video_filename"]),
                heygen_status=str(heygen_result["status"]),
                heygen_video_id=str(heygen_result["video_id"]),
                heygen_avatar_id=str(heygen_result["avatar_id"]),
                heygen_use_avatar_iv_model=bool(heygen_result["use_avatar_iv_model"]),
                heygen_request_snapshot=dict(heygen_result.get("request_snapshot") or {}),
                heygen_response_snapshot=dict(heygen_result.get("response_snapshot") or {}),
                seed=body.seed,
                avatar_id=body.avatar_id,
                use_avatar_iv_model=body.use_avatar_iv_model,
                targets=targets,
            )
            job = await job_service.get_job(body.job_id)
            script_json = _as_script_json(job.get("script_json"))

        running_publish_script = _merge_script_json_with_wf13_state(
            script_json,
            state="running_publish",
            seed=body.seed,
            avatar_id=body.avatar_id,
            use_avatar_iv_model=body.use_avatar_iv_model,
            targets=targets,
            last_error="",
        )
        await job_service.update_job(body.job_id, script_json=running_publish_script, error_message="")
        await _wf13_notify(channel_id, "📤 WF-13 자동 YouTube 업로드를 시작합니다.")
        current_video_url = _resolve_internal_media_url(str(job.get("video_url") or ""))
        current_audio_url = _resolve_internal_media_url(str(job.get("audio_url") or ""))
        subtitle_script_text = _get_subtitle_script_text(script_json)
        media_names = script_json.get("media_names") if isinstance(script_json.get("media_names"), dict) else {}
        video_filename = _normalize_video_filename(body.job_id, media_names.get("video_filename"), script_json)
        publish_result = await _wf13_publish_auto_with_retry(
            job_id=body.job_id,
            channel_id=channel_id,
            video_url=current_video_url,
            audio_url=current_audio_url,
            video_filename=video_filename,
            title=_build_publish_title(body.job_id, video_filename, subtitle_script_text, str(job.get("concept_text") or "")),
            description=_build_publish_description(subtitle_script_text),
            caption=_build_publish_caption(subtitle_script_text),
            subtitle_script_text=subtitle_script_text,
            script_json=script_json,
            seed=body.seed,
            avatar_id=body.avatar_id,
            use_avatar_iv_model=body.use_avatar_iv_model,
            targets=targets,
        )
        latest_job = await job_service.get_job(body.job_id)
        latest_script_json = _as_script_json(latest_job.get("script_json") if latest_job else script_json)
        completed_script = _merge_script_json_with_wf13_state(
            latest_script_json,
            state="completed",
            seed=body.seed,
            avatar_id=body.avatar_id,
            use_avatar_iv_model=body.use_avatar_iv_model,
            targets=targets,
            last_completed_stage="publish",
            last_error="",
        )
        await job_service.update_job(body.job_id, script_json=completed_script, error_message="")
        if publish_result.get("summary_text"):
            await _wf13_notify(channel_id, str(publish_result["summary_text"]))
        return {
            "job_id": body.job_id,
            "status": "completed",
            "final_status": publish_result.get("final_status", ""),
            "failure_stage": "",
            "error_type": "",
            "retryable": False,
            "should_stop_workflow": False,
            "delivered_video_to_discord": False,
        }
    except Exception as e:
        logger.exception("[wf13] failed job_id=%s", body.job_id)
        wf13_error = e if isinstance(e, Wf13PipelineError) else None
        failure_stage = wf13_error.failure_stage if wf13_error else ""
        error_type = wf13_error.error_type if wf13_error else ""
        retryable = wf13_error.retryable if wf13_error else False
        should_stop_workflow = wf13_error.should_stop_workflow if wf13_error else False
        blocked_reason = wf13_error.blocked_reason if wf13_error else ""
        user_notified = wf13_error.user_notified if wf13_error else False
        failed_state = "blocked" if blocked_reason else "failed"
        latest_job = await job_service.get_job(body.job_id)
        latest_script_json = _as_script_json(latest_job.get("script_json") if latest_job else script_json)
        if error_type == "youtube_upload_limit_exceeded" and latest_job is not None:
            try:
                current_video_url = _resolve_internal_media_url(str(latest_job.get("video_url") or ""))
                if current_video_url:
                    await _wf13_notify_publish_limit(channel_id, body.job_id, current_video_url)
                    user_notified = True
            except Exception as notify_err:
                logger.warning("[wf13] publish-limit notify failed job_id=%s err=%s", body.job_id, notify_err)
        failed_script = _merge_script_json_with_wf13_state(
            latest_script_json,
            state=failed_state,
            seed=body.seed,
            avatar_id=body.avatar_id,
            use_avatar_iv_model=body.use_avatar_iv_model,
            targets=targets,
            last_error=str(e),
            last_completed_stage=failure_stage if failure_stage == "publish" else str(_get_wf13_state(latest_script_json).get("last_completed_stage") or ""),
            retryable=retryable,
            blocked_reason=blocked_reason,
        )
        await job_service.transition_status(body.job_id, "PUBLISH_FAILED")
        await job_service.update_job(body.job_id, script_json=failed_script, error_message=f"WF-13 failed: {e}")
        if blocked_reason == "heygen_insufficient_credit":
            await _wf13_notify(channel_id, "⚠️ WF-13 자동 영상 생성이 중단되었습니다. HeyGen API credit이 부족합니다. 충전 후 다시 실행해야 합니다.")
        if not user_notified:
            await _wf13_notify(channel_id, f"❌ WF-13 자동 파이프라인 실패\nJob ID: {body.job_id[:8]}\n사유: {_clip(str(e), limit=220)}")
        return {
            "job_id": body.job_id,
            "status": failed_state,
            "detail": str(e),
            "failure_stage": failure_stage,
            "error_type": error_type,
            "retryable": retryable,
            "should_stop_workflow": should_stop_workflow,
            "blocked_reason": blocked_reason,
            "delivered_video_to_discord": bool(user_notified and error_type == "youtube_upload_limit_exceeded"),
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
        requested_targets = _normalize_publish_targets(body.targets)
        existing_posts = await job_service.list_platform_posts(body.job_id)
        published_targets: set[str] = set()
        for post in existing_posts:
            platform = str(post.get("platform") or "").strip().lower()
            status_value = _normalized_platform_status(post.get("status"))
            if platform in {"youtube", "instagram"} and status_value == "published":
                published_targets.add(platform)
        targets = [target for target in requested_targets if target not in published_targets]
        skipped_targets = [target for target in requested_targets if target in published_targets]
        if not targets:
            logger.info(
                "[discord] video_action=approved skipped_all_already_published job_id=%s requested=%s",
                body.job_id,
                ",".join(requested_targets),
            )
            await _discord_adapter.send_text_message(
                channel_id,
                "ℹ️ 선택한 플랫폼은 이미 업로드 완료 상태입니다. 중복 업로드를 건너뜁니다.",
            )
            return {
                "job_id": body.job_id,
                "action": "already_published",
                "requested_targets": requested_targets,
                "skipped_targets": skipped_targets,
            }

        current_status = str(job.get("status") or "")
        if current_status == "PUBLISHING":
            stale_minutes = max(1, int(settings.publish_stale_minutes))
            age_seconds = _publish_age_seconds(job.get("updated_at"))
            is_stale = age_seconds is not None and age_seconds >= (stale_minutes * 60)
            if is_stale:
                logger.warning(
                    "[discord] video_action=approved stale_publishing_recovered job_id=%s age_seconds=%s",
                    body.job_id,
                    int(age_seconds or 0),
                )
                await job_service.transition_status(body.job_id, "WAITING_VIDEO_APPROVAL")
                await _discord_adapter.send_text_message(
                    channel_id,
                    "⚠️ 이전 SNS 업로드가 지연 상태로 감지되어 복구했습니다. 플랫폼 버튼을 다시 눌러 업로드를 시작해주세요.",
                )
                return {
                    "job_id": body.job_id,
                    "action": "publishing_recovered",
                    "requested_targets": requested_targets,
                    "pending_targets": targets,
                    "stale_age_seconds": int(age_seconds or 0),
                }
            logger.info(
                "[discord] video_action=approved skipped_already_publishing job_id=%s pending=%s",
                body.job_id,
                ",".join(targets),
            )
            await _discord_adapter.send_text_message(
                channel_id,
                "⏳ 이미 SNS 업로드가 진행 중입니다. 잠시 후 결과 메시지를 확인해주세요.",
            )
            return {
                "job_id": body.job_id,
                "action": "already_publishing",
                "requested_targets": requested_targets,
                "pending_targets": targets,
                "skipped_targets": skipped_targets,
            }

        status_transitioned = False
        rollback_status = current_status or "WAITING_VIDEO_APPROVAL"
        if current_status != "PUBLISHING":
            await job_service.transition_status(body.job_id, "PUBLISHING")
            status_transitioned = True

        try:
            # 최종 승인 시점에는 gateway가 직접 업로드하지 않고 WF-08에 SNS 업로드를 위임한다.
            video_url = job.get("video_url", "")
            if isinstance(video_url, str) and video_url.startswith("s3://"):
                video_url = presign_s3_uri(video_url)
            audio_url = job.get("audio_url", "")
            if not str(audio_url or "").strip():
                raise HTTPException(status_code=400, detail="No audio_url found in job")
            if isinstance(audio_url, str) and audio_url.startswith("s3://"):
                audio_url = presign_s3_uri(audio_url)

            script_json = _as_script_json(job.get("script_json"))
            media_names = script_json.get("media_names") if isinstance(script_json.get("media_names"), dict) else {}
            video_filename = _normalize_video_filename(body.job_id, media_names.get("video_filename"), script_json)
            subtitle_script_text = str(script_json.get("subtitle_script_text") or "").strip()
            if not subtitle_script_text:
                raise HTTPException(
                    status_code=400,
                    detail="subtitle_script_text is required for youtube caption text",
                )
            requested_publish_title = str(getattr(body, "publish_title", "") or "").strip()
            publish_title = (
                requested_publish_title
                if requested_publish_title
                else _build_publish_title(body.job_id, video_filename, subtitle_script_text, str(job.get("concept_text") or ""))
            )
            publish_description = _build_publish_description(subtitle_script_text)
            publish_caption = _build_publish_caption(subtitle_script_text)

            await n8n_service.call_wf08_sns_upload(
                body.job_id,
                video_url,
                str(audio_url),
                channel_id,
                targets,
                video_filename=video_filename,
                title=publish_title,
                description=publish_description,
                caption=publish_caption,
                subtitle_script_text=subtitle_script_text,
            )
        except Exception as e:
            if status_transitioned:
                try:
                    await job_service.transition_status(body.job_id, rollback_status)
                except Exception as rollback_error:
                    logger.warning(
                        "rollback status failed job_id=%s target_status=%s err=%s",
                        body.job_id,
                        rollback_status,
                        rollback_error,
                    )
            if isinstance(e, HTTPException):
                logger.error("video_action approved failed job_id=%s detail=%s", body.job_id, e.detail)
                raise
            logger.error("call_wf08_sns_upload failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=502, detail=f"WF-08 trigger failed: {e}")

        if skipped_targets:
            await _discord_adapter.send_text_message(
                channel_id,
                f"ℹ️ 이미 게시된 플랫폼({', '.join(skipped_targets)})은 제외하고 업로드를 시작합니다.",
            )
        logger.info("[discord] video_action=approved job_id=%s targets=%s", body.job_id, ",".join(targets))
        return {
            "job_id": body.job_id,
            "action": "approved",
            "requested_targets": requested_targets,
            "targets": targets,
            "skipped_targets": skipped_targets,
        }

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
                    downstream_intent=_get_tts_downstream_intent(script_json),
                )
                await _discord_adapter.send_text_message(channel_id, "🔊 TTS 후보 3개를 다시 생성합니다...")
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
            downstream_intent="tts_only",
        )
        await _discord_adapter.send_text_message(
            channel_id,
            "🔊 TTS 후보 3개 생성을 시작합니다. 마음에 드는 후보를 선택한 뒤 승인하면 WF-12(HeyGen)로 진행됩니다.",
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
        logger.info(
            "[avatar-policy] ignore requested avatar_id on report_to_video job_id=%s (env-first policy)",
            body.job_id,
        )
    resolved_avatar_id, avatar_source = await _resolve_heygen_avatar_id(job)

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
            downstream_intent="video_prepare",
        )
        await _discord_adapter.send_text_message(
            channel_id,
            "🎬 영상 제작 준비를 시작합니다. TTS 후보 3개 중 하나를 선택한 뒤 일반 승인 또는 고화질 승인을 선택하고, 최종 확인 후 영상을 생성하세요.",
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
            downstream_intent="tts_only",
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
    configured_avatar_id = (body.avatar_id or "").strip()
    config_error = ""
    if not configured_avatar_id:
        try:
            configured_avatar_id = str(_parse_heygen_avatar_options_from_env()[0]["avatar_id"])
        except HTTPException as e:
            config_error = str(e.detail)
    avatars_payload = await _heygen_get_json("/v2/avatars")

    avatars_data = avatars_payload.get("data") if isinstance(avatars_payload.get("data"), dict) else {}
    avatars = avatars_data.get("avatars") if isinstance(avatars_data.get("avatars"), list) else []

    avatar_check: dict[str, object] = {
        "avatar_id": configured_avatar_id,
        "configured": bool(configured_avatar_id),
        "exists": False,
        "name": "",
        "config_error": config_error,
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


@app.post("/internal/seedlab-start")
async def seedlab_start(_: AuthDep, body: SeedLabStartRequest) -> dict:
    run_id = f"seedlab-{uuid.uuid4().hex[:12]}"
    samples = _SEEDLAB_DUP_SAMPLES if body.dup else _SEEDLAB_DEFAULT_SAMPLES
    takes_per_seed = _SEEDLAB_DUP_TAKES if body.dup else _SEEDLAB_DEFAULT_TAKES
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, int(settings.seedlab_link_ttl_seconds)))
    created = await job_service.create_seed_lab_run(
        run_id=run_id,
        discord_user_id=body.messenger_user_id,
        discord_channel_id=body.messenger_channel_id,
        dataset_path="./scripts/seed_lab_dataset.local.json",
        seed_list_raw=(body.seeds or "").strip(),
        dup_mode=bool(body.dup),
        samples=samples,
        takes_per_seed=takes_per_seed,
        concurrency=_SEEDLAB_SAMPLE_CONCURRENCY,
        run_dir=f"/app/runtime/seed-lab/runs/{run_id}",
        signed_link_expires_at=expires_at,
    )
    response = await _seedlab_service_request(
        "POST",
        "/internal/runs",
        json_body={
            "run_id": run_id,
            "seeds": (body.seeds or "").strip(),
            "dup": bool(body.dup),
        },
    )
    if response.status_code >= 400:
        response_detail = response.text
        try:
            parsed = response.json()
            if isinstance(parsed, dict) and parsed.get("detail"):
                response_detail = str(parsed["detail"])
        except Exception:
            pass
        await job_service.update_seed_lab_run(run_id, status="failed", last_error=response_detail[:800])
        raise HTTPException(status_code=502, detail=f"seedlab create failed: {response_detail[:300]}")
    token = _build_seedlab_signed_token(run_id=run_id, user_id=body.messenger_user_id, expires_at=expires_at)
    base_url = (settings.seedlab_public_base_url or "").strip().rstrip("/")
    link = f"{base_url}/seedlab/r/{token}/" if base_url else f"/seedlab/r/{token}/"
    created = await job_service.update_seed_lab_run(run_id, status="queued")
    return {
        "run_id": run_id,
        "status": str((created or {}).get("status") or "queued"),
        "seedlab_url": link,
        "expires_at": expires_at.isoformat(),
        "samples": samples,
        "takes_per_seed": takes_per_seed,
    }


@app.post("/internal/seedlab-refresh-link")
async def seedlab_refresh_link(_: AuthDep, body: SeedLabRefreshLinkRequest) -> dict:
    run = await job_service.get_seed_lab_run(body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Seed Lab run not found")
    user_id = (body.messenger_user_id or "").strip() or str(run.get("discord_user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="discord user id is required")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, int(settings.seedlab_link_ttl_seconds)))
    await job_service.update_seed_lab_run(body.run_id, signed_link_expires_at=expires_at)
    token = _build_seedlab_signed_token(run_id=body.run_id, user_id=user_id, expires_at=expires_at)
    base_url = (settings.seedlab_public_base_url or "").strip().rstrip("/")
    link = f"{base_url}/seedlab/r/{token}/" if base_url else f"/seedlab/r/{token}/"
    return {"run_id": body.run_id, "seedlab_url": link, "expires_at": expires_at.isoformat()}


@app.post("/internal/cost-viewer-link")
async def cost_viewer_link(_: AuthDep, body: dict[str, Any]) -> dict:
    user_id = str(body.get("messenger_user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="discord user id is required")
    ttl_seconds = max(60, int(settings.cost_viewer_link_ttl_seconds or settings.seedlab_link_ttl_seconds))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    return {
        "cost_viewer_url": _build_cost_viewer_link(user_id=user_id, expires_at=expires_at),
        "expires_at": expires_at.isoformat(),
    }


@app.post("/internal/seedlab-progress")
async def seedlab_progress(_: AuthDep, body: SeedLabProgressRequest) -> dict:
    run = await job_service.get_seed_lab_run(body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Seed Lab run not found")

    incoming_stage = str(body.stage or body.status or "")
    incoming_generated = int(body.generated_count or 0)
    incoming_evaluated = int(body.evaluated_count or 0)
    incoming_failed = int(body.failed_count or 0)
    incoming_total = int(body.total_count or 0)

    current_stage = str(run.get("progress_last_stage") or "")
    current_generated = int(run.get("progress_last_generated_count") or 0)
    current_evaluated = int(run.get("progress_last_evaluated_count") or 0)
    current_failed = int(run.get("progress_last_failed_count") or 0)
    current_total = int(run.get("progress_last_total_count") or 0)

    should_update_message = (
        incoming_stage != current_stage
        or incoming_generated != current_generated
        or incoming_evaluated != current_evaluated
        or incoming_failed != current_failed
        or incoming_total != current_total
        or str(run.get("progress_message_id") or "").strip() == ""
    )

    updated_run = await job_service.update_seed_lab_run(
        body.run_id,
        status=str(body.status or run.get("status") or "queued"),
        last_error=str(body.last_error or ""),
        progress_last_stage=incoming_stage,
        progress_last_generated_count=incoming_generated,
        progress_last_evaluated_count=incoming_evaluated,
        progress_last_failed_count=incoming_failed,
        progress_last_total_count=incoming_total,
    )
    run_for_message = updated_run or run
    if should_update_message and _discord_adapter is not None:
        progress_text = _format_seedlab_progress_text(run_for_message, body)
        await _ensure_seedlab_progress_message(run_for_message, progress_text)
    return {"ok": True, "updated": should_update_message}


async def _seedlab_proxy_request(token: str, suffix: str, request: Request) -> Response:
    payload = _verify_seedlab_signed_token(token)
    run_id = str(payload["run_id"])
    run = await job_service.get_seed_lab_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Seed Lab run not found")
    clean_suffix = suffix.lstrip("/")
    service_path = f"/runs/{run_id}/{clean_suffix}" if clean_suffix else f"/runs/{run_id}/"
    body = await request.body()
    headers: dict[str, str] = {}
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    upstream = await _seedlab_service_request(request.method, service_path, content=body if body else None, headers=headers)
    content_type = upstream.headers.get("content-type") or "application/octet-stream"
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=content_type.split(";", 1)[0],
        headers={"Content-Type": content_type},
    )


@app.get("/seedlab/r/{token}/")
async def seedlab_proxy_root(token: str, request: Request) -> Response:
    return await _seedlab_proxy_request(token, "", request)


@app.get("/seedlab/r/{token}/{suffix:path}")
async def seedlab_proxy_get(token: str, suffix: str, request: Request) -> Response:
    return await _seedlab_proxy_request(token, suffix, request)


@app.post("/seedlab/r/{token}/{suffix:path}")
async def seedlab_proxy_post(token: str, suffix: str, request: Request) -> Response:
    return await _seedlab_proxy_request(token, suffix, request)


@app.put("/seedlab/r/{token}/{suffix:path}")
async def seedlab_proxy_put(token: str, suffix: str, request: Request) -> Response:
    return await _seedlab_proxy_request(token, suffix, request)


@app.post("/internal/jobs")
async def list_jobs(_: AuthDep, body: ListJobsRequest) -> dict:
    """Discord 수동 명령 보조용 최근 job 목록을 반환한다."""
    purpose = (body.purpose or "all").strip().lower()
    if purpose not in ("all", "tts", "heygen"):
        raise HTTPException(status_code=400, detail="purpose must be one of: all, tts, heygen")

    require_script = purpose == "tts"
    require_audio = purpose == "heygen"
    rows = await job_service.list_recent_jobs_in_channel(
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


@app.post("/internal/cost-events")
async def ingest_cost_event(_: AuthDep, body: CostEventIngestRequest) -> dict:
    inserted = await cost_service.ingest_event(body.model_dump())
    return {"status": "ok", "inserted": bool(inserted)}


def _parse_ymd(text: str) -> Optional[date]:
    value = (text or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid date format: {value} (expected YYYY-MM-DD)")


async def _costs_jobs_payload(
    *,
    from_date: str = "",
    to_date: str = "",
    q: str = "",
    status_filter: str = "",
    limit: int = 50,
    offset: int = 0,
    subject_type: str = "all",
    sort_by: str = "updated_at",
    sort_dir: str = "desc",
) -> dict:
    resolved_limit = max(1, min(int(limit), int(settings.cost_max_list_limit)))
    return await cost_service.list_jobs_summary(
        from_date=_parse_ymd(from_date),
        to_date=_parse_ymd(to_date),
        q=q,
        status=status_filter,
        limit=resolved_limit,
        offset=offset,
        subject_type=subject_type,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


async def _costs_job_detail_payload(job_id: str) -> dict:
    try:
        return await cost_service.get_job_detail(job_id)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


async def _costs_export_payload(
    *,
    job_id: str = "",
    from_date: str = "",
    to_date: str = "",
    subject_type: str = "all",
) -> JSONResponse:
    payload = await cost_service.export_payload(
        job_id=job_id.strip(),
        from_date=_parse_ymd(from_date),
        to_date=_parse_ymd(to_date),
        subject_type=(subject_type or "all").strip().lower(),
    )
    filename = f"cost-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _cost_viewer_html(api_base_path: str) -> str:
    safe_api_base_path = json.dumps(api_base_path.rstrip("/"), ensure_ascii=False)
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Hari 비용 보기</title>
  <style>
    :root {
      --bg:#f5f7fa; --panel:#ffffff; --border:#d1d9e0;
      --text:#1a2332; --muted:#6b7a8d;
      --accent:#0e7c6b; --accent-soft:#e0f5f1;
      --warn:#b45309; --warn-soft:#fef3c7;
      --danger:#c0392b; --danger-soft:#fde8e8;
      --info:#1e40af; --info-soft:#dbeafe;
      --success:#166534; --success-soft:#dcfce7;
    }
    *{box-sizing:border-box;margin:0;padding:0;}
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);font-size:13px;line-height:1.5;}
    .container{max-width:1440px;margin:0 auto;padding:16px;}
    h1{font-size:18px;font-weight:700;margin-bottom:14px;}
    .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.05);}
    .filter-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
    .filter-row label{font-size:11px;color:var(--muted);white-space:nowrap;}
    input[type="date"],input[type="text"],select{height:32px;padding:0 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:#fff;color:var(--text);}
    input[type="text"]{min-width:200px;}
    button{height:32px;padding:0 14px;border:1px solid var(--border);border-radius:6px;font-size:12px;cursor:pointer;background:#fff;color:var(--text);white-space:nowrap;}
    button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
    button:disabled{opacity:.4;cursor:default;}
    button.sm{height:26px;padding:0 10px;font-size:11px;}
    #statusBar{font-size:11px;color:var(--muted);margin-top:8px;min-height:16px;}
    .summary-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:12px;}
    .card{border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:#fafbfc;}
    .card .clabel{font-size:10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;}
    .card .cusd{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:4px;}
    .card .ckrw{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;margin-top:2px;}
    .table-wrap{overflow-x:auto;}
    table{width:100%;border-collapse:collapse;font-size:12px;}
    thead{background:#f0f2f5;}
    th{padding:8px 10px;text-align:left;font-weight:600;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;border-bottom:2px solid var(--border);white-space:nowrap;}
    td{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle;}
    tr:last-child td{border-bottom:none;}
    tr:hover td{background:#f8f9fc;}
    .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;}
    .badge.job{background:#dbeafe;color:#1d4ed8;}
    .badge.operation{background:#d1fae5;color:#065f46;}
    .badge.published,.badge.success{background:var(--success-soft);color:var(--success);}
    .badge.approved{background:var(--info-soft);color:var(--info);}
    .badge.waiting{background:var(--warn-soft);color:var(--warn);}
    .badge.failed{background:var(--danger-soft);color:var(--danger);}
    .badge.actual{background:var(--success-soft);color:var(--success);}
    .badge.estimated{background:var(--warn-soft);color:var(--warn);}
    .badge.fixed{background:var(--info-soft);color:var(--info);}
    .badge.missing{background:var(--danger-soft);color:var(--danger);}
    .badge.default{background:#e5e7eb;color:#374151;}
    .stage-row{display:flex;gap:8px;align-items:center;}
    .si{display:inline-flex;flex-direction:column;align-items:center;gap:1px;}
    .si .ico{font-size:14px;line-height:1;}
    .si .mark{font-size:11px;font-weight:800;line-height:1;}
    .mark.ok{color:#16a34a;}
    .mark.fail{color:#dc2626;}
    .mark.skip{color:#d1d5db;}
    .cost-cell .cusd2{font-weight:600;font-variant-numeric:tabular-nums;}
    .cost-cell .ckrw2{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums;}
    .mono{font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;font-size:11px;}
    .pagination{display:flex;align-items:center;justify-content:space-between;padding:10px 4px 2px;font-size:12px;color:var(--muted);}
    .page-btns{display:flex;gap:6px;align-items:center;}
    #detailPanel{display:none;}
    .detail-header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:12px;}
    .detail-header .did{font-family:monospace;font-size:12px;word-break:break-all;color:var(--muted);}
    .cost-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:12px;}
    .cs-card{border:1px solid var(--border);border-radius:7px;padding:8px 10px;background:#fafbfc;}
    .cs-card .cs-label{font-size:10px;color:var(--muted);font-weight:600;text-transform:uppercase;}
    .cs-card .cs-val{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums;}
    .cs-card .cs-krw{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;}
    .breakdown-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;margin-bottom:12px;}
    .bd-box{border:1px solid var(--border);border-radius:7px;padding:8px 10px;}
    .bd-box h4{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:6px;}
    .bd-row{display:flex;justify-content:space-between;font-size:11px;padding:2px 0;gap:8px;}
    .bd-key{color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .bd-val{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;}
    #eventsSection h3{font-size:13px;margin-bottom:8px;}
    #eventsSection table th{font-size:10px;}
    #eventsSection table td{font-size:11px;padding:7px 8px;}
    .toggle-btn{margin-top:8px;}
    #rawJson{display:none;margin-top:8px;font-size:11px;background:#f8fafc;border:1px solid var(--border);border-radius:7px;padding:10px;overflow:auto;max-height:400px;white-space:pre;font-family:monospace;}
    .section-title{font-size:13px;font-weight:600;margin-bottom:8px;}
  </style>
</head>
<body>
<div class="container">
  <h1>&#x1F4B0; 비용 보기</h1>

  <div class="panel">
    <div class="filter-row">
      <label>시작일</label>
      <input type="date" id="fromDate" />
      <label>종료일</label>
      <input type="date" id="toDate" />
      <input type="text" id="queryText" placeholder="&#xC81C;&#xBAA9; / ID &#xAC80;&#xC0C9;" />
      <select id="subjectTypeFilter">
        <option value="all">&#xC804;&#xCCB4; &#xAD6C;&#xBD84;</option>
        <option value="job">&#xC791;&#xC5C5;</option>
        <option value="operation">&#xC6B4;&#xC601; &#xC791;&#xC5C5;</option>
      </select>
      <select id="statusFilter">
        <option value="">&#xC0C1;&#xD0DC; &#xC804;&#xCCB4;</option>
        <option value="PUBLISHED">&#xBC1C;&#xD589; &#xC644;&#xB8CC;</option>
        <option value="REPORT_READY">&#xB9AC;&#xD3EC;&#xD2B8; &#xC900;&#xBE44; &#xC644;&#xB8CC;</option>
        <option value="WAITING_VIDEO_APPROVAL">&#xC601;&#xC0C1; &#xC2B9;&#xC778; &#xB300;&#xAE30;</option>
        <option value="APPROVED">&#xC2B9;&#xC778; &#xC644;&#xB8CC;</option>
        <option value="PUBLISH_FAILED">&#xBC1C;&#xD589; &#xC2E4;&#xD328;</option>
        <option value="FAILED">&#xC2E4;&#xD328;</option>
      </select>
      <label>&#xC815;&#xB82C;</label>
      <select id="sortBySelect">
        <option value="updated_at" selected>&#xC218;&#xC815; &#xC2DC;&#xAC01;</option>
        <option value="created_at">&#xC0DD;&#xC131; &#xC2DC;&#xAC01;</option>
        <option value="main_cost_usd">&#xCD1D; &#xBE44;&#xC6A9;</option>
        <option value="estimated_cost_usd">&#xC608;&#xC0C1; &#xBE44;&#xC6A9;</option>
      </select>
      <select id="sortDirSelect">
        <option value="desc" selected>&#xB0B4;&#xB9BC;&#xCC28;&#xC21C;</option>
        <option value="asc">&#xC624;&#xB984;&#xCC28;&#xC21C;</option>
      </select>
      <select id="pageSizeSelect">
        <option value="25">25&#xAC1C;</option>
        <option value="50" selected>50&#xAC1C;</option>
        <option value="100">100&#xAC1C;</option>
      </select>
      <button class="primary" id="searchBtn">&#x1F50D; &#xC870;&#xD68C;</button>
      <button id="exportRangeBtn">&#x2B07; &#xC6D0;&#xBCF8; &#xB370;&#xC774;&#xD130; &#xB0B4;&#xBCF4;&#xB0B4;&#xAE30;</button>
    </div>
    <div id="statusBar"></div>
    <div class="summary-row">
      <div class="card">
        <div class="clabel">&#xCD1D; &#xBE44;&#xC6A9;</div>
        <div class="cusd" id="mainCostUsd">$0.000000</div>
        <div class="ckrw" id="mainCostKrw">&#x20A9;0</div>
      </div>
      <div class="card">
        <div class="clabel">&#xC77C;&#xC77C; &#xBE44;&#xC6A9; &#xC608;&#xC0C1; (3&#xAC1C; &#xC601;&#xC0C1; &#xAE30;&#xC900;)</div>
        <div class="cusd" id="estimatedCostUsd">$0.000000</div>
        <div class="ckrw" id="estimatedCostKrw">&#x20A9;0</div>
        <div class="ckrw" id="estimatedCostBasis">&#xD45C;&#xBCF8; &#xC5C6;&#xC74C;</div>
      </div>
      <div class="card">
        <div class="clabel">&#xBE44;&#xC6A9; &#xC815;&#xBCF4; &#xC5C6;&#xC74C;</div>
        <div class="cusd" id="missingCount" style="color:var(--danger);">0</div>
        <div class="ckrw">&#xC9D1;&#xACC4; &#xC548; &#xB41C; &#xD56D;&#xBAA9; &#xC218;</div>
      </div>
      <div class="card">
        <div class="clabel">&#xC870;&#xD68C;&#xB41C; &#xD56D;&#xBAA9;</div>
        <div class="cusd" id="rowCount">0&#xAC74;</div>
        <div class="ckrw" id="totalCount">&#xC804;&#xCCB4; 0&#xAC74;</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width:70px;">&#xAD6C;&#xBD84;</th>
            <th style="width:88px;">ID</th>
            <th>&#xC81C;&#xBAA9;</th>
            <th style="width:120px;">&#xB2E8;&#xACC4;</th>
            <th style="width:110px;">&#xC0C1;&#xD0DC;</th>
            <th style="width:130px;">&#xCD1D; &#xBE44;&#xC6A9;</th>
            <th style="width:130px;">&#xC608;&#xC0C1; &#xBE44;&#xC6A9;</th>
            <th style="width:108px;">&#xC0DD;&#xC131; &#xC2DC;&#xAC01; (KST)</th>
            <th style="width:108px;">&#xC218;&#xC815; &#xC2DC;&#xAC01; (KST)</th>
            <th style="width:88px;">&#xBCF4;&#xAE30;</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div class="pagination">
      <span id="pageInfo" style="color:var(--muted);">-</span>
      <div class="page-btns">
        <button id="prevBtn" disabled>&#x2190; &#xC774;&#xC804;</button>
        <button id="nextBtn" disabled>&#xB2E4;&#xC74C; &#x2192;</button>
      </div>
    </div>
  </div>

  <div class="panel" id="detailPanel">
    <div class="detail-header">
      <strong id="detailTitle" style="font-size:15px;"></strong>
      <span class="did" id="detailId"></span>
      <span id="detailStatus"></span>
      <span style="color:var(--muted);font-size:11px;" id="detailDate"></span>
    </div>
    <div class="cost-summary" id="detailCostSummary"></div>
    <div class="section-title">&#xBE44;&#xC6A9; &#xC138;&#xBD80; &#xB0B4;&#xC5ED;</div>
    <div class="breakdown-grid" id="detailBreakdown"></div>
    <div id="eventsSection">
      <h3>&#xC791;&#xC5C5; &#xC9C4;&#xD589; &#xB0B4;&#xC5ED;</h3>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:110px;">&#xC2DC;&#xAC01; (KST)</th>
              <th style="width:70px;">&#xB2E8;&#xACC4;</th>
              <th style="width:130px;">&#xC791;&#xC5C5;</th>
              <th style="width:100px;">&#xC11C;&#xBE44;&#xC2A4;</th>
              <th style="width:80px;">&#xCC98;&#xB9AC; &#xACB0;&#xACFC;</th>
              <th style="width:80px;">&#xBE44;&#xC6A9; &#xBC29;&#xC2DD;</th>
              <th style="width:100px;">&#xB2EC;&#xB7EC;</th>
              <th style="width:90px;">&#xC6D0;&#xD654;</th>
              <th style="width:75px;">&#xAC78;&#xB9B0; &#xC2DC;&#xAC04;</th>
              <th>&#xC624;&#xB958;</th>
            </tr>
          </thead>
          <tbody id="eventRows"></tbody>
        </table>
      </div>
    </div>
    <button class="sm toggle-btn" id="toggleRawBtn">{ } &#xC6D0;&#xBCF8; &#xB370;&#xC774;&#xD130; &#xBCF4;&#xAE30;</button>
    <pre id="rawJson"></pre>
  </div>
</div>

<script>
  var apiBase = __API_BASE_PATH__;
  var currentOffset = 0;
  var currentTotal  = 0;
  var STAGE_LABELS = {
    video: "\uC601\uC0C1 \uC0DD\uC131",
    infra: "\uC778\uD504\uB77C",
    script: "\uB300\uBCF8 \uC791\uC131",
    notebook: "\uB9AC\uD3EC\uD2B8 \uC0DD\uC131",
    tts: "\uC74C\uC131 \uC0DD\uC131",
    publish: "\uBC30\uD3EC \uC900\uBE44",
    content: "\uCF58\uD150\uCE20 \uC815\uB9AC"
  };
  var PROCESS_LABELS = {
    heygen_generate: "\uC544\uBC14\uD0C0 \uC601\uC0C1 \uC0DD\uC131",
    daily_fixed_allocation: "\uC77C\uC77C \uACE0\uC815\uBE44 \uBC30\uBD84",
    cua_generate_report: "\uBE44\uC6A9 \uB9AC\uD3EC\uD2B8 \uC0DD\uC131",
    tts_script_rewrite: "\uC74C\uC131\uC6A9 \uB300\uBCF8 \uB2E4\uB4EC\uAE30",
    subtitle_script_rewrite: "\uC790\uB9C9\uC6A9 \uB300\uBCF8 \uB2E4\uB4EC\uAE30",
    generate_tts_audio: "\uC74C\uC131 \uD30C\uC77C \uC0DD\uC131",
    youtube_caption_asr: "\uC720\uD29C\uBE0C \uC790\uB9C9 \uCD94\uCD9C",
    content_metadata_generate: "\uCF58\uD150\uCE20 \uC815\uBCF4 \uC815\uB9AC",
    content_embedding_generate: "\uCF58\uD150\uCE20 \uAC80\uC0C9\uC6A9 \uB370\uC774\uD130 \uC0DD\uC131",
    hardburn_subtitle: "\uC790\uB9C9 \uC785\uD788\uAE30"
  };
  var PROVIDER_LABELS = {
    heygen: "HeyGen",
    aws_fixed: "AWS",
    openai: "OpenAI",
    runpod_tts: "Runpod",
    runpod_fixed: "Runpod",
    ffmpeg: "FFmpeg"
  };
  var API_KEY_FAMILY_LABELS = {
    heygen: "HeyGen",
    infra_fixed: "\uC778\uD504\uB77C \uACE0\uC815\uBE44",
    rewrite: "\uB300\uBCF8 \uB2E4\uB4EC\uAE30",
    cua_generate_report: "\uBE44\uC6A9 \uB9AC\uD3EC\uD2B8 \uC0DD\uC131",
    tts_generation: "\uC74C\uC131 \uC0DD\uC131",
    youtube_asr: "\uC720\uD29C\uBE0C \uC790\uB9C9 \uCD94\uCD9C",
    youtube_caption_asr: "\uC720\uD29C\uBE0C \uC790\uB9C9 \uCD94\uCD9C",
    content_metadata: "\uCF58\uD150\uCE20 \uC815\uBCF4 \uC815\uB9AC",
    content_metadata_generate: "\uCF58\uD150\uCE20 \uC815\uBCF4 \uC815\uB9AC",
    content_embedding: "\uCF58\uD150\uCE20 \uAC80\uC0C9\uC6A9 \uB370\uC774\uD130 \uC0DD\uC131",
    content_embedding_generate: "\uCF58\uD150\uCE20 \uAC80\uC0C9\uC6A9 \uB370\uC774\uD130 \uC0DD\uC131",
    hardburn_subtitle: "\uC790\uB9C9 \uC785\uD788\uAE30"
  };
  var PRICING_KIND_LABELS = {
    actual: "\uC2E4\uCE21",
    estimated: "\uC608\uC0C1",
    fixed: "\uACE0\uC815",
    missing: "\uC9D1\uACC4 \uC548 \uB428"
  };
  var JOB_STATUS_LABELS = {
    PUBLISHED: "\uBC1C\uD589 \uC644\uB8CC",
    REPORT_READY: "\uB9AC\uD3EC\uD2B8 \uC900\uBE44 \uC644\uB8CC",
    WAITING_VIDEO_APPROVAL: "\uC601\uC0C1 \uC2B9\uC778 \uB300\uAE30",
    APPROVED: "\uC2B9\uC778 \uC644\uB8CC",
    FAILED: "\uC2E4\uD328",
    PUBLISH_FAILED: "\uBC1C\uD589 \uC2E4\uD328"
  };
  var EVENT_STATUS_LABELS = {
    success: "\uC644\uB8CC",
    failed: "\uC2E4\uD328"
  };
  var SUBJECT_TYPE_LABELS = {
    job: "\uC791\uC5C5",
    operation: "\uC6B4\uC601 \uC791\uC5C5"
  };

  function pageSize() { return parseInt(document.getElementById("pageSizeSelect").value) || 50; }
  function q(id) { return document.getElementById(id); }
  function num(v) { var n = Number(v); return isNaN(n) ? 0 : n; }
  function fmtUsd(v) { return "$" + num(v).toFixed(6); }
  function fmtKrw(v) { return "\u20A9" + Math.round(num(v)).toLocaleString("ko-KR"); }
  function shortText(v, max) { var s = String(v || ""); return s.length <= max ? s : s.slice(0, max - 1) + "\u2026"; }
  function hasOwn(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }
  function humanizeCode(code) {
    var text = String(code || "").trim();
    if (!text) return "\u2013";
    var normalized = text.replace(/[_-]+/g, " ");
    normalized = normalized.replace(/\bapi\b/gi, "API");
    normalized = normalized.replace(/\btts\b/gi, "TTS");
    normalized = normalized.replace(/\basr\b/gi, "ASR");
    normalized = normalized.replace(/\bcua\b/gi, "CUA");
    normalized = normalized.replace(/\bffmpeg\b/gi, "FFmpeg");
    normalized = normalized.replace(/\bopenai\b/gi, "OpenAI");
    normalized = normalized.replace(/\bheygen\b/gi, "HeyGen");
    normalized = normalized.replace(/\baws\b/gi, "AWS");
    normalized = normalized.replace(/\brunpod\b/gi, "Runpod");
    return normalized;
  }
  function labelFor(code, labels) {
    var key = String(code || "").trim();
    if (!key) return "\u2013";
    return hasOwn(labels, key) ? labels[key] : humanizeCode(key);
  }
  function formatStage(code) { return labelFor(code, STAGE_LABELS); }
  function formatProcess(code) { return labelFor(code, PROCESS_LABELS); }
  function formatProvider(code) { return labelFor(code, PROVIDER_LABELS); }
  function formatApiKeyFamily(code) { return labelFor(code, API_KEY_FAMILY_LABELS); }
  function formatPricingKind(code) { return labelFor(String(code || "").toLowerCase(), PRICING_KIND_LABELS); }
  function formatSubjectType(code) { return labelFor(String(code || "").toLowerCase(), SUBJECT_TYPE_LABELS); }
  function formatJobStatus(code) { return labelFor(String(code || "").toUpperCase(), JOB_STATUS_LABELS); }
  function formatEventStatus(code) { return labelFor(String(code || "").toLowerCase(), EVENT_STATUS_LABELS); }
  function parseOperationLabel(subject) {
    var key = String(subject.subject_key || "").trim();
    if (/^(operation|legacy):/.test(key)) {
      var parts = key.split(":");
      if (parts.length >= 3) return formatProcess(parts[1]) + " / " + formatProvider(parts[2]);
    }
    var label = String(subject.subject_label || subject.topic_text || "").trim();
    var labelParts = label.split(/\s*\/\s*/);
    if (labelParts.length === 2) return formatProcess(labelParts[0]) + " / " + formatProvider(labelParts[1]);
    return label ? humanizeCode(label) : "";
  }
  function displaySubjectLabel(subject) {
    if (String(subject.subject_type || "").toLowerCase() === "operation") {
      return parseOperationLabel(subject) || "\uC6B4\uC601 \uC791\uC5C5";
    }
    return String(subject.subject_label || subject.topic_text || subject.subject_key || "");
  }
  function toKST(iso) {
    if (!iso) return "\u2013";
    try { return new Date(iso).toLocaleString("ko-KR", { timeZone:"Asia/Seoul", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false }); }
    catch(e) { return String(iso).slice(0,16); }
  }
  function badge(text, cls) { return '<span class="badge ' + cls + '">' + text + '</span>'; }
  function statusBadge(s) {
    var u = String(s || "").toUpperCase();
    if (!u) return "\u2013";
    if (u === "PUBLISHED") return badge(formatJobStatus(u), "published");
    if (u === "REPORT_READY") return badge(formatJobStatus(u), "approved");
    if (u === "WAITING_VIDEO_APPROVAL") return badge(formatJobStatus(u), "waiting");
    if (u === "APPROVED") return badge(formatJobStatus(u), "approved");
    if (u === "FAILED" || u === "PUBLISH_FAILED") return badge(formatJobStatus(u), "failed");
    return badge(formatJobStatus(u), "default");
  }
  function pricingBadge(pk) {
    var p = String(pk || "").toLowerCase();
    return ["actual","estimated","fixed","missing"].indexOf(p) >= 0 ? badge(formatPricingKind(p), p) : badge(formatPricingKind(p), "default");
  }
  function isIgnoredMissingEvent(ev) {
    var provider = String(ev && ev.provider || "").trim().toLowerCase();
    var status = String(ev && ev.status || "").trim().toLowerCase();
    var pricingKind = String(ev && ev.pricing_kind || "").trim().toLowerCase() || "missing";
    var pricingSource = String(ev && ev.pricing_source || "").trim().toLowerCase() || "unavailable";
    return provider === "runpod_tts" && status === "failed" && pricingKind === "missing" && pricingSource === "unavailable";
  }
  function pricingCell(ev) {
    return isIgnoredMissingEvent(ev) ? "\u2013" : pricingBadge(ev && ev.pricing_kind);
  }
  function statusEvBadge(st) {
    var s = String(st || "").toLowerCase();
    if (s === "success") return badge(formatEventStatus(s), "success");
    if (s === "failed")  return badge(formatEventStatus(s), "failed");
    return badge(formatEventStatus(s), "default");
  }
  function durFmt(ms) {
    var n = Number(ms);
    if (!ms || isNaN(n)) return "\u2013";
    if (n < 1000)  return n + "ms";
    if (n < 60000) return (n / 1000).toFixed(1) + "s";
    return Math.floor(n / 60000) + "m" + Math.round((n % 60000) / 1000) + "s";
  }
  function stageIcons(rec) {
    function mark(s, f) {
      if (num(s) > 0) return '<span class="mark ok">\u2713</span>';
      if (num(f) > 0) return '<span class="mark fail">\u2717</span>';
      return '<span class="mark skip">\u2013</span>';
    }
    return '<div class="stage-row">'
      + '<div class="si"><span class="ico">\U0001F4DD</span>' + mark(rec.script_success, rec.script_failed) + '</div>'
      + '<div class="si"><span class="ico">\U0001F399</span>' + mark(rec.tts_success,    rec.tts_failed)    + '</div>'
      + '<div class="si"><span class="ico">\U0001F3AC</span>' + mark(rec.video_success,  rec.video_failed)  + '</div>'
      + '</div>';
  }

  async function fetchJson(url) {
    var resp = await fetch(url);
    if (!resp.ok) { var t = await resp.text(); throw new Error("HTTP " + resp.status + ": " + t.slice(0,200)); }
    return resp.json();
  }
  function buildListUrl(offset) {
    var p = new URLSearchParams();
    var from = q("fromDate").value, to = q("toDate").value;
    var qs = q("queryText").value.trim(), st = q("statusFilter").value.trim();
    var stype = q("subjectTypeFilter").value.trim();
    var sortBy = q("sortBySelect").value.trim();
    var sortDir = q("sortDirSelect").value.trim();
    if (from)  p.set("from", from);
    if (to)    p.set("to", to);
    if (qs)    p.set("q", qs);
    if (st)    p.set("status", st);
    if (stype) p.set("subject_type", stype);
    if (sortBy) p.set("sort_by", sortBy);
    if (sortDir) p.set("sort_dir", sortDir);
    p.set("limit", pageSize()); p.set("offset", offset);
    return apiBase + "/api/jobs?" + p.toString();
  }
  function buildExportUrl() {
    var p = new URLSearchParams();
    var from = q("fromDate").value, to = q("toDate").value;
    var stype = q("subjectTypeFilter").value.trim();
    if (from) p.set("from", from); if (to) p.set("to", to); if (stype) p.set("subject_type", stype);
    return apiBase + "/api/export?" + p.toString();
  }

  async function loadRows(offset) {
    q("statusBar").textContent = "\uC870\uD68C \uC911\u2026";
    q("rows").innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:24px;">\uBD88\uB7EC\uC624\uB294 \uC911\u2026</td></tr>';
    try {
      var data = await fetchJson(buildListUrl(offset));
      currentOffset = offset; currentTotal = num(data.total);
      var items = Array.isArray(data.items) ? data.items : [];
      var mu = 0, mk = 0, ms = 0;
      q("rows").innerHTML = "";
      for (var i = 0; i < items.length; i++) {
        var rec = items[i]; var byP = rec.by_pricing_kind || {};
        var mainKrw = num((byP.actual||{}).cost_krw) + num((byP.fixed||{}).cost_krw);
        var estKrw  = num((byP.estimated||{}).cost_krw);
        mu += num(rec.main_cost_usd); mk += mainKrw;
        ms += num(rec.missing_cost_event_count);
        var shortKey = String(rec.subject_key || rec.job_id || "").slice(0, 8);
        var tr = document.createElement("tr");
        tr.innerHTML =
          '<td>' + badge(formatSubjectType(rec.subject_type || "job"), rec.subject_type || "job") + '</td>'
          + '<td><span class="mono" title="' + (rec.subject_key||"") + '">' + shortKey + '</span></td>'
          + '<td>' + shortText(displaySubjectLabel(rec), 60) + '</td>'
          + '<td>' + stageIcons(rec) + '</td>'
          + '<td>' + statusBadge(rec.status) + '</td>'
          + '<td class="cost-cell"><div class="cusd2">' + fmtUsd(rec.main_cost_usd) + '</div><div class="ckrw2">' + fmtKrw(mainKrw) + '</div></td>'
          + '<td class="cost-cell"><div class="cusd2">' + fmtUsd(rec.estimated_cost_usd) + '</div><div class="ckrw2">' + fmtKrw(estKrw) + '</div></td>'
          + '<td style="color:var(--muted);">' + toKST(rec.created_at) + '</td>'
          + '<td style="color:var(--muted);">' + toKST(rec.updated_at) + '</td>'
          + '<td></td>';
        (function(r, row) {
          var td = row.cells[9];
          var db = document.createElement("button"); db.className = "sm primary"; db.textContent = "\uC0C1\uC138";
          db.onclick = function() { loadDetail(String(r.subject_key || r.job_id || "")); };
          var eb = document.createElement("button"); eb.className = "sm"; eb.textContent = "\uC6D0\uBCF8"; eb.style.marginLeft = "4px";
          eb.onclick = function() { window.open(apiBase + "/api/export?job_id=" + encodeURIComponent(r.subject_key || r.job_id || ""), "_blank"); };
          td.appendChild(db); td.appendChild(eb);
        })(rec, tr);
        q("rows").appendChild(tr);
      }
      q("mainCostUsd").textContent = fmtUsd(mu); q("mainCostKrw").textContent = fmtKrw(mk);
      var dailyEstimate = data.daily_estimate || {};
      if (num(dailyEstimate.sample_count) > 0) {
        q("estimatedCostUsd").textContent = fmtUsd(dailyEstimate.estimated_daily_cost_usd);
        q("estimatedCostKrw").textContent = fmtKrw(dailyEstimate.estimated_daily_cost_krw);
      } else {
        q("estimatedCostUsd").textContent = "\u2013";
        q("estimatedCostKrw").textContent = "\uD45C\uBCF8 \uC5C6\uC74C";
      }
      q("estimatedCostBasis").textContent = dailyEstimate.basis || "\uD45C\uBCF8 \uC5C6\uC74C";
      q("missingCount").textContent = String(ms);
      q("rowCount").textContent = items.length + "\uAC74";
      q("totalCount").textContent = "\uC804\uCCB4 " + currentTotal + "\uAC74";
      var ps = pageSize(), fr = currentOffset + 1, to2 = Math.min(currentOffset + items.length, currentTotal);
      q("pageInfo").textContent = currentTotal > 0 ? fr + "\u2013" + to2 + " / \uC804\uCCB4 " + currentTotal + "\uAC74" : "\uACB0\uACFC \uC5C6\uC74C";
      q("prevBtn").disabled = currentOffset <= 0;
      q("nextBtn").disabled = (currentOffset + ps) >= currentTotal;
      q("statusBar").textContent = "\uB9C8\uC9C0\uB9C9 \uC870\uD68C: " + new Date().toLocaleTimeString("ko-KR");
    } catch(e) {
      q("statusBar").textContent = "\uC624\uB958: " + e.message;
      q("rows").innerHTML = '<tr><td colspan="10" style="color:var(--danger);text-align:center;padding:16px;">' + e.message + '</td></tr>';
    }
  }

  function formatBreakdownKey(kind, key) {
    if (kind === "stage") return formatStage(key);
    if (kind === "process") return formatProcess(key);
    if (kind === "provider") return formatProvider(key);
    if (kind === "api_key_family") return formatApiKeyFamily(key);
    if (kind === "pricing_kind") return formatPricingKind(key);
    return humanizeCode(key);
  }
  function renderBreakdown(title, obj, kind) {
    var entries = Object.entries(obj || {}).sort(function(a,b){ return num(b[1].cost_usd)-num(a[1].cost_usd); });
    var rows = entries.length
      ? entries.map(function(p){ return '<div class="bd-row"><span class="bd-key">' + shortText(formatBreakdownKey(kind, p[0]),30) + '</span><span class="bd-val">' + fmtUsd(p[1].cost_usd) + ' \u00B7 ' + p[1].count + '\uAC74</span></div>'; }).join("")
      : '<div class="bd-row"><span class="bd-key" style="color:var(--muted);">\uC5C6\uC74C</span></div>';
    return '<div class="bd-box"><h4>' + title + '</h4>' + rows + '</div>';
  }

  async function loadDetail(subjectKey) {
    q("detailPanel").style.display = "block";
    q("detailPanel").scrollIntoView({ behavior:"smooth", block:"start" });
    q("detailTitle").textContent = "\uBD88\uB7EC\uC624\uB294 \uC911\u2026";
    q("detailId").textContent    = subjectKey;
    q("detailStatus").innerHTML  = ""; q("detailDate").textContent = "";
    q("detailCostSummary").innerHTML = ""; q("detailBreakdown").innerHTML = "";
    q("eventRows").innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:16px;">\uBD88\uB7EC\uC624\uB294 \uC911\u2026</td></tr>';
    q("rawJson").style.display = "none"; q("toggleRawBtn").textContent = "{ } \uC6D0\uBCF8 \uB370\uC774\uD130 \uBCF4\uAE30";
    try {
      var det = await fetchJson(apiBase + "/api/jobs/" + encodeURIComponent(subjectKey));
      var sub = det.subject || {}, sum = det.summary || {};
      var events = Array.isArray(det.events) ? det.events : [];
      q("detailTitle").textContent = shortText(displaySubjectLabel(sub) || subjectKey, 70);
      q("detailId").textContent    = subjectKey;
      q("detailStatus").innerHTML  = statusBadge(sub.status);
      q("detailDate").textContent  = toKST(sub.created_at);
      var byP = sum.by_pricing_kind || {};
      var mKrw = num((byP.actual||{}).cost_krw) + num((byP.fixed||{}).cost_krw);
      q("detailCostSummary").innerHTML =
          '<div class="cs-card"><div class="cs-label">\uCD1D \uBE44\uC6A9</div><div class="cs-val">' + fmtUsd(sum.main_cost_usd) + '</div><div class="cs-krw">' + fmtKrw(mKrw) + '</div></div>'
        + '<div class="cs-card"><div class="cs-label">\uC2E4\uCE21 \uBE44\uC6A9</div><div class="cs-val">' + fmtUsd(sum.actual_cost_usd) + '</div><div class="cs-krw">' + fmtKrw((byP.actual||{}).cost_krw) + '</div></div>'
        + '<div class="cs-card"><div class="cs-label">\uACE0\uC815 \uBE44\uC6A9</div><div class="cs-val">' + fmtUsd(sum.fixed_cost_usd) + '</div><div class="cs-krw">' + fmtKrw((byP.fixed||{}).cost_krw) + '</div></div>'
        + '<div class="cs-card"><div class="cs-label">\uC608\uC0C1 \uBE44\uC6A9</div><div class="cs-val">' + fmtUsd(sum.estimated_cost_usd) + '</div><div class="cs-krw">' + fmtKrw((byP.estimated||{}).cost_krw) + '</div></div>'
        + '<div class="cs-card"><div class="cs-label">\uBE44\uC6A9 \uC815\uBCF4 \uC5C6\uC74C</div><div class="cs-val" style="color:var(--danger);">' + (sum.missing_cost_event_count||0) + '\uAC74</div><div class="cs-krw">&nbsp;</div></div>';
      q("detailBreakdown").innerHTML =
          renderBreakdown("\uB2E8\uACC4\uBCC4 \uBE44\uC6A9", sum.by_stage, "stage")
        + renderBreakdown("\uC791\uC5C5\uBCC4 \uBE44\uC6A9", sum.by_process, "process")
        + renderBreakdown("\uC11C\uBE44\uC2A4\uBCC4 \uBE44\uC6A9", sum.by_provider, "provider")
        + renderBreakdown("\uBE44\uC6A9 \uBD84\uB958\uBCC4", sum.by_api_key_family, "api_key_family")
        + renderBreakdown("\uBE44\uC6A9 \uACC4\uC0B0 \uBC29\uC2DD", sum.by_pricing_kind, "pricing_kind");
      if (!events.length) {
        q("eventRows").innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:12px;">\uC774\uBCA4\uD2B8 \uC5C6\uC74C</td></tr>';
      } else {
        var html = "";
        for (var j = 0; j < events.length; j++) {
          var ev = events[j];
          html += '<tr>'
            + '<td style="white-space:nowrap;">' + toKST(ev.created_at)                    + '</td>'
            + '<td>' + formatStage(ev.stage) + '</td>'
            + '<td>' + formatProcess(ev.process) + '</td>'
            + '<td>' + formatProvider(ev.provider) + '</td>'
            + '<td>' + statusEvBadge(ev.status)  + '</td>'
            + '<td>' + pricingCell(ev) + '</td>'
            + '<td class="mono">' + (ev.cost_usd != null ? fmtUsd(ev.cost_usd) : "\u2013") + '</td>'
            + '<td class="mono">' + (ev.cost_krw != null ? fmtKrw(ev.cost_krw) : "\u2013") + '</td>'
            + '<td style="white-space:nowrap;">' + durFmt(ev.duration_ms) + '</td>'
            + '<td style="color:var(--danger);max-width:200px;word-break:break-word;">' + shortText(String(ev.error_message||"").trim(), 80) + '</td>'
            + '</tr>';
        }
        q("eventRows").innerHTML = html;
      }
      q("rawJson").textContent = JSON.stringify(det, null, 2);
    } catch(e) { q("detailTitle").textContent = "\uC624\uB958: " + e.message; }
  }

  q("searchBtn").addEventListener("click",    function() { currentOffset = 0; loadRows(0).catch(function(e){ q("statusBar").textContent = String(e); }); });
  q("sortBySelect").addEventListener("change", function() { currentOffset = 0; loadRows(0).catch(function(e){ q("statusBar").textContent = String(e); }); });
  q("sortDirSelect").addEventListener("change", function() { currentOffset = 0; loadRows(0).catch(function(e){ q("statusBar").textContent = String(e); }); });
  q("prevBtn").addEventListener("click",      function() { loadRows(Math.max(0, currentOffset - pageSize())).catch(function(e){ q("statusBar").textContent = String(e); }); });
  q("nextBtn").addEventListener("click",      function() { loadRows(currentOffset + pageSize()).catch(function(e){ q("statusBar").textContent = String(e); }); });
  q("exportRangeBtn").addEventListener("click", function() { window.open(buildExportUrl(), "_blank"); });
  q("toggleRawBtn").addEventListener("click", function() {
    var el = q("rawJson"), vis = el.style.display !== "none";
    el.style.display = vis ? "none" : "block";
    q("toggleRawBtn").textContent = vis ? "{ } \uC6D0\uBCF8 \uB370\uC774\uD130 \uBCF4\uAE30" : "{ } \uC6D0\uBCF8 \uB370\uC774\uD130 \uC228\uAE30\uAE30";
  });
  loadRows(0).catch(function(e){ q("statusBar").textContent = String(e); });
</script>
</body>
</html>""".replace("__API_BASE_PATH__", safe_api_base_path)


@app.get("/costs", response_class=HTMLResponse)
async def costs_page(_: CostViewerAuthDep) -> HTMLResponse:
    return HTMLResponse(_cost_viewer_html("/costs"))


@app.get("/costs/api/jobs")
async def costs_jobs(
    _: CostViewerAuthDep,
    from_date: str = Query("", alias="from"),
    to_date: str = Query("", alias="to"),
    q: str = Query("", alias="q"),
    status_filter: str = Query("", alias="status"),
    subject_type: str = Query("all", alias="subject_type"),
    sort_by: str = Query("updated_at", alias="sort_by"),
    sort_dir: str = Query("desc", alias="sort_dir"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    return await _costs_jobs_payload(
        from_date=from_date,
        to_date=to_date,
        q=q,
        status_filter=status_filter,
        subject_type=subject_type,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )


@app.get("/costs/api/jobs/{job_id}")
async def costs_job_detail(_: CostViewerAuthDep, job_id: str) -> dict:
    return await _costs_job_detail_payload(job_id)


@app.get("/costs/api/export")
async def costs_export(
    _: CostViewerAuthDep,
    job_id: str = Query(""),
    from_date: str = Query("", alias="from"),
    to_date: str = Query("", alias="to"),
    subject_type: str = Query("all", alias="subject_type"),
) -> JSONResponse:
    return await _costs_export_payload(
        job_id=job_id,
        from_date=from_date,
        to_date=to_date,
        subject_type=subject_type,
    )


@app.get("/cost/r/{token}/", response_class=HTMLResponse)
async def costs_signed_page(token: str) -> HTMLResponse:
    _verify_cost_viewer_signed_token(token)
    return HTMLResponse(_cost_viewer_html(f"/cost/r/{token}"))


@app.get("/cost/r/{token}/api/jobs")
async def costs_signed_jobs(
    token: str,
    from_date: str = Query("", alias="from"),
    to_date: str = Query("", alias="to"),
    q: str = Query("", alias="q"),
    status_filter: str = Query("", alias="status"),
    subject_type: str = Query("all", alias="subject_type"),
    sort_by: str = Query("updated_at", alias="sort_by"),
    sort_dir: str = Query("desc", alias="sort_dir"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    _verify_cost_viewer_signed_token(token)
    return await _costs_jobs_payload(
        from_date=from_date,
        to_date=to_date,
        q=q,
        status_filter=status_filter,
        subject_type=subject_type,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )


@app.get("/cost/r/{token}/api/jobs/{job_id}")
async def costs_signed_job_detail(token: str, job_id: str) -> dict:
    _verify_cost_viewer_signed_token(token)
    return await _costs_job_detail_payload(job_id)


@app.get("/cost/r/{token}/api/export")
async def costs_signed_export(
    token: str,
    job_id: str = Query(""),
    from_date: str = Query("", alias="from"),
    to_date: str = Query("", alias="to"),
    subject_type: str = Query("all", alias="subject_type"),
) -> JSONResponse:
    _verify_cost_viewer_signed_token(token)
    return await _costs_export_payload(
        job_id=job_id,
        from_date=from_date,
        to_date=to_date,
        subject_type=subject_type,
    )


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
