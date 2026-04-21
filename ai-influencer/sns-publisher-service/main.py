import logging
import os
import tempfile
import time
import hashlib
import json
import math
import re
import wave
from datetime import datetime, timezone
from difflib import SequenceMatcher
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib import request as urllib_request

import psycopg2
import requests
from fastapi import FastAPI, HTTPException
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from openai import OpenAI
from pydantic import BaseModel, Field
from psycopg2.extras import Json


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
INSTAGRAM_GRAPH_API_BASE = "https://graph.facebook.com"
ALLOWED_TARGETS = {"youtube", "instagram"}
OPENAI_TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024
YOUTUBE_CAPTION_OPENING_LINE = "보리들 안녕? 내일, 주식 사야할 것 같은데?"
YOUTUBE_CAPTION_ENDING_LINE = "그럼, 어떤 주식 사야할지 알겠지?"
REQUIRED_ENV_BY_TARGET = {
    "youtube": [
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
    ],
    "instagram": [
        "INSTAGRAM_PAGE_ACCESS_TOKEN",
        "INSTAGRAM_IG_USER_ID",
    ],
}


class PublishRequest(BaseModel):
    job_id: str
    video_url: str
    audio_url: str = ""
    targets: list[str] = Field(default_factory=list)
    title: str = ""
    description: str = ""
    caption: str = ""
    subtitle_script_text: str = ""
    video_filename: str = ""


class CaptionArtifactRequest(BaseModel):
    job_id: str
    audio_url: str = ""
    subtitle_script_text: str = ""
    tts_script_text: str = ""
    selected_tts_timing: dict = Field(default_factory=dict)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


DEFAULT_TRANSCRIPTION_COST_USD_PER_MINUTE: dict[str, float] = {
    "whisper-1": 0.006,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-transcribe-diarize": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
}


def _env_float(name: str, default: float = 0.0) -> float:
    raw = str(os.environ.get(name, default) or default).strip()
    try:
        return float(raw)
    except Exception:
        return float(default)


def _default_transcription_rate(model: str) -> float:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return 0.0
    if normalized in DEFAULT_TRANSCRIPTION_COST_USD_PER_MINUTE:
        return DEFAULT_TRANSCRIPTION_COST_USD_PER_MINUTE[normalized]
    for key, value in DEFAULT_TRANSCRIPTION_COST_USD_PER_MINUTE.items():
        if normalized.startswith(key):
            return value
    return 0.0


def _estimate_youtube_asr_cost_usd(duration_sec: float, *, model: str = "") -> float | None:
    rate = _env_float("YOUTUBE_ASR_COST_USD_PER_MINUTE", _default_transcription_rate(model or _asr_primary_model()))
    if duration_sec <= 0 or rate <= 0:
        return None
    return (duration_sec / 60.0) * rate


def _post_cost_event(payload: dict[str, object]) -> None:
    gateway_url = (os.environ.get("COST_TRACKING_GATEWAY_URL", "http://messenger-gateway:8080") or "").strip().rstrip("/")
    secret = (os.environ.get("GATEWAY_INTERNAL_SECRET", "") or "").strip()
    if not gateway_url or not secret:
        return
    request = urllib_request.Request(
        f"{gateway_url}/internal/cost-events",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Secret": secret},
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=15) as response:
        response.read()


def _record_youtube_asr_cost_event(
    *,
    job_id: str,
    context_label: str,
    artifacts: dict[str, Any],
    status: str = "success",
    error_message: str = "",
) -> None:
    usage_json = {
        "asr_model": artifacts.get("asr_model"),
        "word_count": artifacts.get("word_count"),
        "segment_count": artifacts.get("segment_count"),
        "audio_size_bytes": artifacts.get("audio_size_bytes"),
        "audio_duration_sec": artifacts.get("audio_duration_sec"),
        "alignment_status": artifacts.get("alignment_status"),
        "timing_source": artifacts.get("timing_source"),
        "fallback_reason": artifacts.get("fallback_reason"),
        "context": context_label,
    }
    cost_usd = _estimate_youtube_asr_cost_usd(
        float(artifacts.get("audio_duration_sec") or 0.0),
        model=str(artifacts.get("asr_model") or ""),
    )
    payload = {
        "job_id": job_id,
        "stage": "publish",
        "process": "youtube_caption_asr",
        "provider": "openai",
        "attempt_no": 1,
        "status": status,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "usage_json": usage_json,
        "raw_response_json": {"request_json": artifacts.get("request_json") or {}, "context": context_label},
        "cost_usd": cost_usd,
        "pricing_kind": "estimated" if cost_usd is not None else "missing",
        "pricing_source": "provider_usage_estimate" if cost_usd is not None else "unavailable",
        "api_key_family": "youtube_asr",
        "subject_type": "job",
        "subject_key": job_id,
        "subject_label": job_id,
        "error_type": "" if not error_message else "youtube_caption_asr_error",
        "error_message": error_message[:500],
        "idempotency_key": (
            f"youtube-asr:{context_label}:{job_id}:{artifacts.get('subtitle_sha256') or 'nohash'}:"
            f"{int(time.time() * 1000)}"
        ),
    }
    try:
        _post_cost_event(payload)
    except Exception as e:
        logger.warning("[cost] youtube asr event post failed job_id=%s context=%s err=%s", job_id, context_label, e)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _missing_env_keys(names: list[str]) -> list[str]:
    missing: list[str] = []
    for name in names:
        if not os.environ.get(name, "").strip():
            missing.append(name)
    return missing


def _target_readiness(target: str) -> dict:
    required = REQUIRED_ENV_BY_TARGET.get(target, [])
    missing = _missing_env_keys(required)
    return {
        "target": target,
        "ready": len(missing) == 0,
        "required_env": required,
        "missing_env": missing,
    }


def _all_target_readiness() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for target in sorted(ALLOWED_TARGETS):
        out[target] = _target_readiness(target)
    return out


def _clip_text(text: str, max_len: int) -> str:
    normalized = " ".join((text or "").strip().split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3].rstrip() + "..."


def _youtube_title(req: PublishRequest) -> str:
    fallback = (req.video_filename or "").rsplit(".", 1)[0].strip() or f"Hari {req.job_id[:8]}"
    return _clip_text(req.title or fallback, 95)


def _youtube_description(req: PublishRequest) -> str:
    return (req.description or req.caption or "").strip()


def _instagram_caption(req: PublishRequest) -> str:
    return (req.caption or req.description or "").strip()


def _build_youtube_caption_text(req: PublishRequest) -> str:
    body_lines = [line.strip() for line in (req.subtitle_script_text or "").splitlines() if line.strip()]
    if not body_lines:
        return ""
    output_lines = list(body_lines)
    if output_lines[0] != YOUTUBE_CAPTION_OPENING_LINE:
        output_lines.insert(0, YOUTUBE_CAPTION_OPENING_LINE)
    if output_lines[-1] != YOUTUBE_CAPTION_ENDING_LINE:
        output_lines.append(YOUTUBE_CAPTION_ENDING_LINE)
    return "\n".join(output_lines).strip()


def _youtube_subtitle_source(req: PublishRequest) -> str:
    # YouTube caption uses subtitle_script_text as body and adds fixed opening/ending lines.
    return _build_youtube_caption_text(req)


def _build_caption_artifact_request_json(
    *,
    subtitle_text: str,
    subtitle_body_text: str,
    original_line_count: int,
    caption_has_opening: bool,
    caption_has_ending: bool,
    subtitle_hash: str,
    asr_model: str,
    audio_size_bytes: int,
    words: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    spoken_sentence_count: int,
    display_sentence_count: int,
    matched_sentence_count: int,
    srt_content: str,
    alignment_status: str,
    cue_count: int,
    timing_source: str,
    fallback_reason: str,
    selected_tts_timing: dict[str, float],
) -> dict[str, Any]:
    return {
        "enabled": True,
        "subtitle_source": "subtitle_script_text+fixed_opening_ending",
        "line_count": display_sentence_count,
        "original_line_count": original_line_count,
        "caption_line_count": display_sentence_count,
        "cue_count": cue_count,
        "caption_has_opening": caption_has_opening,
        "caption_has_ending": caption_has_ending,
        "subtitle_sha256": subtitle_hash,
        "asr_model": asr_model,
        "input_source": "audio_url",
        "audio_size_bytes": audio_size_bytes,
        "word_count": len(words),
        "segment_count": len(segments),
        "timing_source": timing_source,
        "body_line_count": len(_split_alignment_sentences(subtitle_body_text)),
        "spoken_sentence_count": spoken_sentence_count,
        "display_sentence_count": display_sentence_count,
        "matched_sentence_count": matched_sentence_count,
        "alignment_status": alignment_status,
        "fallback_reason": fallback_reason,
        "selected_tts_timing": dict(selected_tts_timing or {}),
        "body_split_mode": _caption_body_split_mode(),
        "body_target_chars": _caption_body_target_chars(),
        "body_hard_max_chars": _caption_body_hard_max_chars(),
        "body_max_chunks_per_sentence": _caption_body_max_chunks_per_sentence(),
        "body_min_cue_ms": _caption_body_min_cue_ms(),
        "srt_preview": srt_content[:800],
    }


def _subtitle_sha256(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _open_db_conn():
    return psycopg2.connect(
        host=_require_env("POSTGRES_HOST"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        database=_require_env("POSTGRES_DB"),
        user=_require_env("POSTGRES_USER"),
        password=_require_env("POSTGRES_PASSWORD"),
    )


def _ensure_schema_sync() -> None:
    conn = _open_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS status TEXT")
            cur.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS platform_post_url TEXT")
            cur.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS error_message TEXT")
            cur.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS request_json JSONB DEFAULT '{}'::jsonb")
            cur.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS response_json JSONB DEFAULT '{}'::jsonb")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS platform_posts_job_platform_uidx ON platform_posts (job_id, platform)"
            )
        conn.commit()
    finally:
        conn.close()


def _persist_platform_result(job_id: str, platform: str, result: dict) -> None:
    conn = _open_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_posts (
                    job_id,
                    platform,
                    platform_post_id,
                    platform_post_url,
                    status,
                    error_message,
                    request_json,
                    response_json,
                    published_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    CASE WHEN %s = 'published' THEN NOW() ELSE NULL END
                )
                ON CONFLICT (job_id, platform) DO UPDATE SET
                    platform_post_id = EXCLUDED.platform_post_id,
                    platform_post_url = EXCLUDED.platform_post_url,
                    status = EXCLUDED.status,
                    error_message = EXCLUDED.error_message,
                    request_json = EXCLUDED.request_json,
                    response_json = EXCLUDED.response_json,
                    published_at = EXCLUDED.published_at
                """,
                [
                    job_id,
                    platform,
                    result.get("platform_post_id", ""),
                    result.get("platform_post_url", ""),
                    result.get("status", "failed"),
                    result.get("error_message", ""),
                    Json(result.get("request_json", {})),
                    Json(result.get("response_json", {})),
                    result.get("status", "failed"),
                ],
            )
        conn.commit()
    finally:
        conn.close()


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _load_job_script_json(job_id: str) -> dict[str, Any]:
    conn = _open_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT script_json FROM jobs WHERE id = %s", [job_id])
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    return _coerce_dict(row[0])


def _normalize_tts_timing(value: Any) -> dict[str, float]:
    raw = _coerce_dict(value)
    required_keys = (
        "opening_duration_sec",
        "opening_gap_sec",
        "body_duration_sec",
        "ending_gap_sec",
        "ending_duration_sec",
        "body_start_sec",
        "body_end_sec",
        "ending_start_sec",
        "final_duration_sec",
    )
    normalized: dict[str, float] = {}
    try:
        for key in required_keys:
            normalized[key] = float(raw[key])
    except Exception:
        return {}
    if normalized["body_start_sec"] <= 0.0:
        return {}
    if normalized["body_end_sec"] <= normalized["body_start_sec"]:
        return {}
    if normalized["ending_start_sec"] < normalized["body_end_sec"]:
        return {}
    if normalized["final_duration_sec"] <= normalized["ending_start_sec"]:
        return {}
    return normalized


def _is_recoverable_caption_alignment_error(error: Exception) -> bool:
    message = str(error or "")
    recoverable_markers = (
        "sentence count mismatch",
        "failed to align sentence",
        "ASR words exhausted before sentence",
        "spoken sentences contain empty alignment target",
    )
    return any(marker in message for marker in recoverable_markers)


def _download_binary_to_temp(url: str, *, suffix: str, timeout: tuple[int, int] = (15, 300)) -> str:
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    temp_file.write(chunk)
        temp_file.flush()
        return temp_file.name
    finally:
        temp_file.close()


def _download_video_to_temp(video_url: str) -> str:
    return _download_binary_to_temp(video_url, suffix=".mp4")


def _download_audio_to_temp(audio_url: str) -> str:
    return _download_binary_to_temp(audio_url, suffix=".wav")


def _youtube_scopes() -> list[str]:
    configured = [scope.strip() for scope in os.environ.get("YOUTUBE_SCOPES", "").split(",") if scope.strip()]
    if configured:
        return configured
    return [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_FORCE_SSL_SCOPE]


def _youtube_caption_enabled() -> bool:
    return _env_bool("YOUTUBE_CAPTION_UPLOAD_ENABLED", True)


def _youtube_caption_language() -> str:
    return os.environ.get("YOUTUBE_CAPTION_LANGUAGE", "ko").strip() or "ko"


def _youtube_caption_name() -> str:
    return os.environ.get("YOUTUBE_CAPTION_NAME", "Korean").strip() or "Korean"


def _youtube_caption_is_draft() -> bool:
    return _env_bool("YOUTUBE_CAPTION_IS_DRAFT", False)


def _youtube_caption_failure_blocks_publish() -> bool:
    return _env_bool("YOUTUBE_CAPTION_FAILURE_BLOCKS_PUBLISH", True)


def _asr_primary_model() -> str:
    return os.environ.get("YOUTUBE_CAPTION_ASR_MODEL", "whisper-1").strip() or "whisper-1"


def _asr_timestamp_model_candidates() -> list[str]:
    return ["whisper-1"]


def _openai_client() -> OpenAI:
    api_key = (
        os.environ.get("OPENAI_API_KEY_YOUTUBE_ASR", "").strip()
        or os.environ.get("OPENAI_FALLBACK_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY_YOUTUBE_ASR or OPENAI_FALLBACK_API_KEY "
            "(or legacy OPENAI_API_KEY) is required"
        )
    return OpenAI(api_key=api_key)


_CAPTION_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CAPTION_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:])\s+")
_CAPTION_TARGET_CHARS = 28
_CAPTION_HARD_MAX_CHARS = 42


def _caption_body_split_mode() -> str:
    return (os.environ.get("YOUTUBE_CAPTION_BODY_SPLIT_MODE", "aggressive").strip() or "aggressive").lower()


def _caption_body_target_chars() -> int:
    default = 10 if _caption_body_split_mode() == "aggressive" else 14
    raw = str(os.environ.get("YOUTUBE_CAPTION_BODY_TARGET_CHARS", default)).strip()
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(6, min(24, value))


def _caption_body_hard_max_chars() -> int:
    default = 16 if _caption_body_split_mode() == "aggressive" else 22
    raw = str(os.environ.get("YOUTUBE_CAPTION_BODY_HARD_MAX_CHARS", default)).strip()
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(_caption_body_target_chars(), min(40, value))


def _caption_body_max_chunks_per_sentence() -> int:
    default = 4 if _caption_body_split_mode() == "aggressive" else 3
    raw = str(os.environ.get("YOUTUBE_CAPTION_BODY_MAX_CHUNKS_PER_SENTENCE", default)).strip()
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(1, min(8, value))


def _caption_body_min_cue_ms() -> int:
    default = 450 if _caption_body_split_mode() == "aggressive" else 550
    raw = str(os.environ.get("YOUTUBE_CAPTION_BODY_MIN_CUE_MS", default)).strip()
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(250, min(2000, value))


def _split_caption_chunk_by_words(chunk: str, *, target_chars: int, hard_max_chars: int) -> list[str]:
    normalized = " ".join((chunk or "").split()).strip()
    if not normalized:
        return []
    if len(normalized) <= hard_max_chars:
        return [normalized]

    words = normalized.split(" ")
    pieces: list[str] = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= target_chars:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = word
        else:
            while len(word) > hard_max_chars:
                pieces.append(word[:target_chars])
                word = word[target_chars:]
            current = word
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def _split_single_subtitle_line(text: str) -> list[str]:
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return []

    sentence_parts = [
        part.strip()
        for part in _CAPTION_SENTENCE_SPLIT_RE.split(normalized)
        if part and part.strip()
    ]
    if not sentence_parts:
        sentence_parts = [normalized]

    output: list[str] = []
    for sentence in sentence_parts:
        if len(sentence) <= _CAPTION_HARD_MAX_CHARS:
            output.append(sentence)
            continue

        clause_parts = [
            part.strip()
            for part in _CAPTION_CLAUSE_SPLIT_RE.split(sentence)
            if part and part.strip()
        ]
        if len(clause_parts) <= 1:
            clause_parts = [sentence]

        for clause in clause_parts:
            output.extend(
                _split_caption_chunk_by_words(
                    clause,
                    target_chars=_CAPTION_TARGET_CHARS,
                    hard_max_chars=_CAPTION_HARD_MAX_CHARS,
                )
            )
    return [piece for piece in output if piece]


def _extract_subtitle_lines(text: str) -> list[str]:
    original_lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    split_lines: list[str] = []
    for line in original_lines:
        split_lines.extend(_split_single_subtitle_line(line))
    return split_lines


def _split_alignment_sentences(text: str) -> list[str]:
    normalized = " ".join((text or "").replace("\r", "\n").split())
    if not normalized:
        return []
    parts = re.findall(r"[^.!?]+[.!?]?", normalized)
    return [part.strip() for part in parts if part and part.strip()]


def _wav_duration_seconds(path: str) -> float:
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return float(frames) / float(rate)
    except Exception:
        return 0.0


def _clean_weight_text(text: str) -> str:
    return "".join(ch for ch in str(text or "") if not ch.isspace())


def _segment_weight(segment: dict[str, Any]) -> float:
    text_weight = float(max(1, len(_clean_weight_text(segment.get("text", "")))))
    duration_weight = float(max(1e-3, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)))) * 6.0
    return max(text_weight, duration_weight)


def _normalize_segments(raw_segments: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in raw_segments or []:
        if isinstance(raw, dict):
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", 0.0))
            text = str(raw.get("text", "")).strip()
        else:
            start = float(getattr(raw, "start", 0.0))
            end = float(getattr(raw, "end", 0.0))
            text = str(getattr(raw, "text", "")).strip()
        if end <= start:
            end = start + 0.01
        normalized.append({"start": start, "end": end, "text": text})
    normalized.sort(key=lambda seg: seg["start"])
    return normalized


def _normalize_words(raw_words: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in raw_words or []:
        if isinstance(raw, dict):
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", 0.0))
            text = str(raw.get("word") or raw.get("text") or "").strip()
        else:
            start = float(getattr(raw, "start", 0.0))
            end = float(getattr(raw, "end", 0.0))
            text = str(getattr(raw, "word", "") or getattr(raw, "text", "")).strip()
        if not text:
            continue
        if end <= start:
            end = start + 0.01
        normalized.append({"start": start, "end": end, "text": text})
    normalized.sort(key=lambda item: item["start"])
    return normalized


def _transcribe_words_and_segments(media_path: str, language: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    client = _openai_client()
    model_candidates = _asr_timestamp_model_candidates()
    seen: set[str] = set()
    last_error = ""
    attempt_errors: list[str] = []

    for model_name in model_candidates:
        if model_name in seen:
            continue
        seen.add(model_name)
        try:
            with open(media_path, "rb") as media_file:
                response = client.audio.transcriptions.create(
                    model=model_name,
                    file=media_file,
                    language=language,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                )
            words = _normalize_words(getattr(response, "words", None))
            segments = _normalize_segments(getattr(response, "segments", None))
            if hasattr(response, "model_dump"):
                dumped = response.model_dump()
                if not words:
                    words = _normalize_words(dumped.get("words"))
                if not segments:
                    segments = _normalize_segments(dumped.get("segments"))
            if isinstance(response, dict):
                if not words:
                    words = _normalize_words(response.get("words"))
                if not segments:
                    segments = _normalize_segments(response.get("segments"))
            if words:
                return words, segments, model_name
            last_error = f"ASR model={model_name} returned no words"
            attempt_errors.append(last_error)
        except Exception as e:
            last_error = f"ASR model={model_name} failed: {e}"
            attempt_errors.append(last_error)
    details = " | ".join(attempt_errors) if attempt_errors else (last_error or "ASR transcription failed")
    raise RuntimeError(details)


def _normalize_alignment_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", str(text or "")).lower()


def _score_alignment(target: str, observed: str) -> float:
    if not target or not observed:
        return -1.0
    ratio = SequenceMatcher(None, target, observed).ratio()
    length_penalty = abs(len(observed) - len(target)) / max(1, len(target)) * 0.25
    overrun_penalty = max(0, len(observed) - len(target)) / max(1, len(target)) * 0.45
    prefix_bonus = 0.08 if (target.startswith(observed) or observed.startswith(target)) else 0.0
    return ratio - length_penalty - overrun_penalty + prefix_bonus


def _align_sentences_to_word_ranges(
    spoken_sentences: list[str],
    words: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    if not spoken_sentences:
        return []
    if not words:
        raise RuntimeError("ASR words are empty")

    targets = [_normalize_alignment_text(sentence) for sentence in spoken_sentences]
    if any(not target for target in targets):
        raise RuntimeError("spoken sentences contain empty alignment target")

    ranges: list[tuple[float, float]] = []
    cursor = 0
    total_words = len(words)

    for idx, target in enumerate(targets):
        remaining_sentences = len(targets) - idx - 1
        if cursor >= total_words:
            raise RuntimeError(f"ASR words exhausted before sentence {idx + 1}/{len(targets)}")
        if idx == len(targets) - 1:
            end_idx = total_words - 1
        else:
            max_end_idx = total_words - remaining_sentences - 1
            best_idx: Optional[int] = None
            best_score = -10.0
            observed = ""
            stale_steps = 0
            hard_limit = max(len(target) + 18, int(len(target) * 2.2))
            for word_idx in range(cursor, max_end_idx + 1):
                observed += _normalize_alignment_text(words[word_idx]["text"])
                if not observed:
                    continue
                score = _score_alignment(target, observed)
                if score > best_score:
                    best_score = score
                    best_idx = word_idx
                    stale_steps = 0
                else:
                    stale_steps += 1
                if len(observed) >= hard_limit and stale_steps >= 4:
                    break
            if best_idx is None:
                raise RuntimeError(f"failed to align sentence {idx + 1}/{len(targets)}")
            end_idx = best_idx
        start_sec = float(words[cursor]["start"])
        end_sec = float(words[end_idx]["end"])
        if end_sec <= start_sec:
            end_sec = start_sec + 0.2
        ranges.append((start_sec, end_sec))
        cursor = end_idx + 1

    return ranges


def _align_sentences_to_word_slices(
    spoken_sentences: list[str],
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not spoken_sentences:
        return []
    if not words:
        raise RuntimeError("ASR words are empty")

    targets = [_normalize_alignment_text(sentence) for sentence in spoken_sentences]
    if any(not target for target in targets):
        raise RuntimeError("spoken sentences contain empty alignment target")

    slices: list[dict[str, Any]] = []
    cursor = 0
    total_words = len(words)

    for idx, target in enumerate(targets):
        remaining_sentences = len(targets) - idx - 1
        if cursor >= total_words:
            raise RuntimeError(f"ASR words exhausted before sentence {idx + 1}/{len(targets)}")
        if idx == len(targets) - 1:
            end_idx = total_words - 1
        else:
            max_end_idx = total_words - remaining_sentences - 1
            best_idx: Optional[int] = None
            best_score = -10.0
            observed = ""
            stale_steps = 0
            hard_limit = max(len(target) + 18, int(len(target) * 2.2))
            for word_idx in range(cursor, max_end_idx + 1):
                observed += _normalize_alignment_text(words[word_idx]["text"])
                if not observed:
                    continue
                score = _score_alignment(target, observed)
                if score > best_score:
                    best_score = score
                    best_idx = word_idx
                    stale_steps = 0
                else:
                    stale_steps += 1
                if len(observed) >= hard_limit and stale_steps >= 4:
                    break
            if best_idx is None:
                raise RuntimeError(f"failed to align sentence {idx + 1}/{len(targets)}")
            end_idx = best_idx
        start_sec = float(words[cursor]["start"])
        end_sec = float(words[end_idx]["end"])
        if end_sec <= start_sec:
            end_sec = start_sec + 0.2
        slices.append(
            {
                "start_sec": start_sec,
                "end_sec": end_sec,
                "start_word_idx": cursor,
                "end_word_idx": end_idx,
            }
        )
        cursor = end_idx + 1
    return slices


def _merge_chunks_to_exact_count(chunks: list[str], chunk_count: int) -> list[str]:
    items = [item.strip() for item in chunks if item and item.strip()]
    if not items:
        return []
    if chunk_count <= 1:
        return [" ".join(items).strip()]
    while len(items) > chunk_count:
        if len(items) == 1:
            break
        merge_idx = min(range(len(items) - 1), key=lambda i: len(items[i]) + len(items[i + 1]))
        items[merge_idx : merge_idx + 2] = [f"{items[merge_idx]} {items[merge_idx + 1]}".strip()]
    return items


def _split_long_piece(piece: str) -> tuple[str, str]:
    normalized = " ".join((piece or "").split()).strip()
    words = normalized.split(" ")
    if len(words) >= 2:
        pivot = max(1, len(words) // 2)
        left = " ".join(words[:pivot]).strip()
        right = " ".join(words[pivot:]).strip()
        if left and right:
            return left, right
    midpoint = max(1, len(normalized) // 2)
    return normalized[:midpoint].strip(), normalized[midpoint:].strip()


def _expand_chunks_to_exact_count(chunks: list[str], chunk_count: int) -> list[str]:
    items = [item.strip() for item in chunks if item and item.strip()]
    if not items:
        return []
    while len(items) < chunk_count:
        split_idx = max(range(len(items)), key=lambda i: len(items[i]))
        target = items[split_idx]
        if len(target) <= 2:
            break
        left, right = _split_long_piece(target)
        if not left or not right:
            break
        items[split_idx : split_idx + 1] = [left, right]
    return items


def _split_sentence_exact(
    text: str,
    *,
    chunk_count: int,
    target_chars: int,
    hard_max_chars: int,
) -> list[str]:
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return []
    if chunk_count <= 1:
        return [normalized]

    base_chunks = _split_single_subtitle_line(normalized)
    chunks = _merge_chunks_to_exact_count(base_chunks, chunk_count)
    chunks = _expand_chunks_to_exact_count(chunks, chunk_count)
    if len(chunks) > chunk_count:
        chunks = _merge_chunks_to_exact_count(chunks, chunk_count)
    if len(chunks) < chunk_count:
        words = normalized.split(" ")
        while len(chunks) < chunk_count and len(words) >= chunk_count:
            segment_size = max(1, len(words) // chunk_count)
            rebuilt: list[str] = []
            cursor = 0
            for idx in range(chunk_count):
                remaining = len(words) - cursor
                remaining_slots = chunk_count - idx
                take = max(1, remaining // remaining_slots)
                rebuilt.append(" ".join(words[cursor : cursor + take]).strip())
                cursor += take
            chunks = [item for item in rebuilt if item]
            break
    if len(chunks) != chunk_count:
        return [normalized]
    return chunks


def _body_sentence_chunk_count(
    *,
    display_sentence: str,
    word_count: int,
    duration_sec: float,
) -> int:
    duration_ms = max(0, int(round(float(duration_sec) * 1000.0)))
    if duration_ms <= 0:
        return 1
    max_chunks = _caption_body_max_chunks_per_sentence()
    min_cue_ms = _caption_body_min_cue_ms()
    by_time = max(1, duration_ms // min_cue_ms)
    text_len = len("".join(ch for ch in str(display_sentence or "") if not ch.isspace()))
    by_chars = max(1, math.ceil(text_len / max(1, _caption_body_target_chars())))
    by_words = max(1, math.ceil(max(1, word_count) / 3))
    desired = max(by_chars, by_words)
    if _caption_body_split_mode() != "aggressive":
        desired = max(1, min(desired, 3))
    return max(1, min(max_chunks, by_time, desired))


def _build_sentence_mapped_srt(
    *,
    spoken_text: str,
    display_text: str,
    words: list[dict[str, Any]],
    final_end_sec: float,
) -> str:
    spoken_sentences = _split_alignment_sentences(spoken_text)
    display_sentences = _split_alignment_sentences(display_text)
    if not spoken_sentences:
        raise RuntimeError("spoken text is empty")
    if not display_sentences:
        raise RuntimeError("display text is empty")
    if len(spoken_sentences) != len(display_sentences):
        raise RuntimeError(
            f"sentence count mismatch spoken={len(spoken_sentences)} display={len(display_sentences)}"
        )

    sentence_slices = _align_sentences_to_word_slices(spoken_sentences, words)
    opening_sentence_count = len(_split_alignment_sentences(YOUTUBE_CAPTION_OPENING_LINE))
    ending_sentence_count = len(_split_alignment_sentences(YOUTUBE_CAPTION_ENDING_LINE))
    cues: list[tuple[float, float, str]] = []
    for idx, display_sentence in enumerate(display_sentences):
        sentence_slice = sentence_slices[idx]
        start_sec = float(sentence_slice["start_sec"])
        end_sec = float(sentence_slice["end_sec"])
        next_start_sec = (
            float(sentence_slices[idx + 1]["start_sec"])
            if idx < len(sentence_slices) - 1
            else float(final_end_sec)
        )
        is_opening = idx < opening_sentence_count
        is_ending = idx >= max(0, len(display_sentences) - ending_sentence_count)
        if is_opening or is_ending:
            cues.append((start_sec, max(end_sec, next_start_sec), display_sentence))
            continue

        start_word_idx = int(sentence_slice["start_word_idx"])
        end_word_idx = int(sentence_slice["end_word_idx"])
        local_words = words[start_word_idx : end_word_idx + 1]
        chunk_count = _body_sentence_chunk_count(
            display_sentence=display_sentence,
            word_count=len(local_words),
            duration_sec=max(0.0, end_sec - start_sec),
        )
        if chunk_count <= 1:
            cues.append((start_sec, max(end_sec, next_start_sec), display_sentence))
            continue

        spoken_chunks = _split_sentence_exact(
            spoken_sentences[idx],
            chunk_count=chunk_count,
            target_chars=_caption_body_target_chars(),
            hard_max_chars=_caption_body_hard_max_chars(),
        )
        display_chunks = _split_sentence_exact(
            display_sentence,
            chunk_count=chunk_count,
            target_chars=_caption_body_target_chars(),
            hard_max_chars=_caption_body_hard_max_chars(),
        )
        if len(spoken_chunks) != chunk_count or len(display_chunks) != chunk_count:
            cues.append((start_sec, max(end_sec, next_start_sec), display_sentence))
            continue

        chunk_slices = _align_sentences_to_word_slices(spoken_chunks, local_words)
        for chunk_idx, display_chunk in enumerate(display_chunks):
            chunk_start = float(chunk_slices[chunk_idx]["start_sec"])
            chunk_end = float(chunk_slices[chunk_idx]["end_sec"])
            if chunk_idx < len(chunk_slices) - 1:
                chunk_end = max(chunk_end, float(chunk_slices[chunk_idx + 1]["start_sec"]))
            else:
                chunk_end = max(chunk_end, next_start_sec)
            cues.append((chunk_start, chunk_end, display_chunk))
    return _render_srt(cues)


def _build_caption_artifacts_for_audio_path(
    *,
    job_id: str,
    audio_path: str,
    subtitle_body_text: str,
    spoken_body_text: str,
    selected_tts_timing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    subtitle_text = _build_youtube_caption_text(
        PublishRequest(
            job_id=job_id,
            video_url="",
            subtitle_script_text=subtitle_body_text,
        )
    )
    original_subtitle_lines = [line.strip() for line in (subtitle_text or "").splitlines() if line.strip()]
    caption_has_opening = bool(original_subtitle_lines and original_subtitle_lines[0] == YOUTUBE_CAPTION_OPENING_LINE)
    caption_has_ending = bool(original_subtitle_lines and original_subtitle_lines[-1] == YOUTUBE_CAPTION_ENDING_LINE)
    subtitle_hash = _subtitle_sha256(subtitle_text)
    if not subtitle_text:
        raise RuntimeError("subtitle_script_text is empty")
    if not spoken_body_text.strip():
        raise RuntimeError("tts_script_text is empty")

    spoken_full_text = _build_youtube_caption_text(
        PublishRequest(
            job_id=job_id,
            video_url="",
            subtitle_script_text=spoken_body_text,
        )
    )
    audio_size_bytes = int(os.path.getsize(audio_path))
    if audio_size_bytes > OPENAI_TRANSCRIBE_MAX_BYTES:
        raise RuntimeError(
            f"audio input too large for ASR: {audio_size_bytes} bytes > {OPENAI_TRANSCRIBE_MAX_BYTES} bytes"
        )

    language = _youtube_caption_language()
    words, segments, asr_model = _transcribe_words_and_segments(audio_path, language)
    audio_duration_sec = _wav_duration_seconds(audio_path) or float(words[-1]["end"])
    final_end_sec = audio_duration_sec
    spoken_sentence_count = len(_split_alignment_sentences(spoken_full_text))
    display_sentence_count = len(_split_alignment_sentences(subtitle_text))
    matched_sentence_count = min(spoken_sentence_count, display_sentence_count)
    normalized_tts_timing = _normalize_tts_timing(selected_tts_timing)
    alignment_status = "ready"
    timing_source = "whisper_word_timestamps"
    fallback_reason = ""
    try:
        srt_content = _build_sentence_mapped_srt(
            spoken_text=spoken_full_text,
            display_text=subtitle_text,
            words=words,
            final_end_sec=final_end_sec,
        )
    except Exception as e:
        if not _is_recoverable_caption_alignment_error(e):
            raise
        if not normalized_tts_timing:
            raise RuntimeError(
                f"{e}; selected_tts_timing is required for sectioned fallback"
            ) from e
        fallback_reason = str(e)
        try:
            srt_content = _build_sectioned_srt(
                subtitle_body_text=subtitle_body_text,
                segments=segments,
                tts_timing=normalized_tts_timing,
            )
        except Exception as fallback_error:
            raise RuntimeError(
                f"{fallback_reason}; sectioned fallback failed: {fallback_error}"
            ) from fallback_error
        alignment_status = "fallback_sectioned"
        timing_source = "selected_tts_timing+asr_segments"
    cue_count = _count_srt_cues(srt_content)
    return {
        "subtitle_text": subtitle_text,
        "spoken_text": spoken_full_text,
        "srt_content": srt_content,
        "asr_model": asr_model,
        "audio_size_bytes": audio_size_bytes,
        "audio_duration_sec": audio_duration_sec,
        "word_count": len(words),
        "segment_count": len(segments),
        "spoken_sentence_count": spoken_sentence_count,
        "display_sentence_count": display_sentence_count,
        "matched_sentence_count": matched_sentence_count,
        "alignment_status": alignment_status,
        "fallback_reason": fallback_reason,
        "timing_source": timing_source,
        "cue_count": cue_count,
        "subtitle_sha256": subtitle_hash,
        "caption_has_opening": caption_has_opening,
        "caption_has_ending": caption_has_ending,
        "original_line_count": len(original_subtitle_lines),
        "request_json": _build_caption_artifact_request_json(
            subtitle_text=subtitle_text,
            subtitle_body_text=subtitle_body_text,
            original_line_count=len(original_subtitle_lines),
            caption_has_opening=caption_has_opening,
            caption_has_ending=caption_has_ending,
            subtitle_hash=subtitle_hash,
            asr_model=asr_model,
            audio_size_bytes=audio_size_bytes,
            words=words,
            segments=segments,
            spoken_sentence_count=spoken_sentence_count,
            display_sentence_count=display_sentence_count,
            matched_sentence_count=matched_sentence_count,
            srt_content=srt_content,
            alignment_status=alignment_status,
            cue_count=cue_count,
            timing_source=timing_source,
            fallback_reason=fallback_reason,
            selected_tts_timing=normalized_tts_timing,
        ),
    }


def _format_srt_timestamp(seconds: float) -> str:
    safe = max(0.0, float(seconds))
    millis = int(round(safe * 1000))
    hours = millis // 3_600_000
    millis %= 3_600_000
    minutes = millis // 60_000
    millis %= 60_000
    secs = millis // 1000
    ms = millis % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _count_srt_cues(srt_content: str) -> int:
    count = 0
    for line in str(srt_content or "").splitlines():
        if "-->" in line:
            count += 1
    return count


def _time_at_weight(weight: float, segments: list[dict[str, Any]], cumulative: list[float]) -> float:
    if not segments:
        return 0.0
    clamped = max(0.0, min(weight, cumulative[-1]))
    prev_cum = 0.0
    for idx, seg in enumerate(segments):
        seg_cum = cumulative[idx]
        if clamped <= seg_cum:
            seg_weight = max(seg_cum - prev_cum, 1e-6)
            ratio = (clamped - prev_cum) / seg_weight
            start = float(seg["start"])
            end = float(seg["end"])
            return start + ((end - start) * ratio)
        prev_cum = seg_cum
    return float(segments[-1]["end"])


def _build_proportional_cues(lines: list[str], *, start_sec: float, end_sec: float) -> list[tuple[float, float, str]]:
    if not lines:
        return []
    section_start = max(0.0, float(start_sec))
    section_end = max(section_start, float(end_sec))
    if section_end <= section_start:
        return [(section_start, section_start + 0.8, lines[0])]

    line_weights = [float(max(1, len(_clean_weight_text(line)))) for line in lines]
    total_line_weight = sum(line_weights) or float(len(lines))
    cues: list[tuple[float, float, str]] = []
    running = 0.0
    floor_start = section_start

    for idx, line in enumerate(lines):
        start_ratio = running / total_line_weight
        running += line_weights[idx]
        end_ratio = running / total_line_weight
        cue_start = section_start + ((section_end - section_start) * start_ratio)
        cue_end = section_start + ((section_end - section_start) * end_ratio)
        cue_start = max(cue_start, floor_start)
        if cue_end <= cue_start:
            cue_end = cue_start + 0.2
        if idx == len(lines) - 1:
            cue_end = max(cue_end, section_end)
        floor_start = cue_end + 0.01
        cues.append((cue_start, cue_end, line))
    return cues


def _crop_segments_to_window(segments: list[dict[str, Any]], *, start_sec: float, end_sec: float) -> list[dict[str, Any]]:
    clipped: list[dict[str, Any]] = []
    for seg in segments:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        if seg_end <= start_sec or seg_start >= end_sec:
            continue
        cropped_start = max(seg_start, start_sec)
        cropped_end = min(seg_end, end_sec)
        if cropped_end <= cropped_start:
            continue
        clipped.append(
            {
                "start": cropped_start,
                "end": cropped_end,
                "text": str(seg.get("text", "")).strip(),
            }
        )
    return clipped


def _build_cues_from_lines_and_segments(
    lines: list[str],
    segments: list[dict[str, Any]],
    *,
    section_start_sec: float,
    section_end_sec: float,
) -> list[tuple[float, float, str]]:
    if not lines:
        return []
    if not segments:
        return _build_proportional_cues(lines, start_sec=section_start_sec, end_sec=section_end_sec)

    segment_weights: list[float] = []
    cumulative: list[float] = []
    running = 0.0
    for seg in segments:
        w = _segment_weight(seg)
        running += w
        segment_weights.append(w)
        cumulative.append(running)
    total_weight = cumulative[-1]

    line_weights = [float(max(1, len(_clean_weight_text(line)))) for line in lines]
    total_line_weight = sum(line_weights) or float(len(lines))

    cues: list[tuple[float, float, str]] = []
    line_running = 0.0
    floor_start = max(section_start_sec, float(segments[0]["start"]))
    section_end = max(section_end_sec, float(segments[-1]["end"]))

    for idx, line in enumerate(lines):
        start_weight = total_weight * (line_running / total_line_weight)
        line_running += line_weights[idx]
        end_weight = total_weight * (line_running / total_line_weight)
        start_sec = _time_at_weight(start_weight, segments, cumulative)
        end_sec = _time_at_weight(end_weight, segments, cumulative)
        start_sec = max(start_sec, floor_start, section_start_sec)
        if end_sec <= start_sec:
            end_sec = start_sec + 0.8
        if idx == len(lines) - 1:
            end_sec = max(end_sec, section_end)
        floor_start = end_sec + 0.01
        cues.append((start_sec, end_sec, line))
    return cues


def _render_srt(cues: list[tuple[float, float, str]]) -> str:
    rows: list[str] = []
    for idx, (start_sec, end_sec, text) in enumerate(cues, start=1):
        rows.append(str(idx))
        rows.append(f"{_format_srt_timestamp(start_sec)} --> {_format_srt_timestamp(end_sec)}")
        rows.append(text)
        rows.append("")
    return "\n".join(rows).strip() + "\n"


def _build_srt_from_subtitle_lines(lines: list[str], segments: list[dict[str, Any]]) -> str:
    if not lines:
        raise RuntimeError("subtitle lines are empty")
    if not segments:
        raise RuntimeError("ASR segments are empty")

    segment_weights: list[float] = []
    cumulative: list[float] = []
    running = 0.0
    for seg in segments:
        w = _segment_weight(seg)
        segment_weights.append(w)
        running += w
        cumulative.append(running)
    total_weight = cumulative[-1]

    line_weights = [float(max(1, len(_clean_weight_text(line)))) for line in lines]
    total_line_weight = sum(line_weights)
    if total_line_weight <= 0:
        total_line_weight = float(len(lines))

    cues: list[tuple[float, float, str]] = []
    line_running = 0.0
    floor_start = float(segments[0]["start"])
    global_end = float(segments[-1]["end"])

    for idx, line in enumerate(lines):
        start_weight = total_weight * (line_running / total_line_weight)
        line_running += line_weights[idx]
        end_weight = total_weight * (line_running / total_line_weight)

        start_sec = _time_at_weight(start_weight, segments, cumulative)
        end_sec = _time_at_weight(end_weight, segments, cumulative)
        start_sec = max(start_sec, floor_start)
        if end_sec <= start_sec:
            end_sec = start_sec + 0.8
        if idx == len(lines) - 1:
            end_sec = max(end_sec, global_end)
        floor_start = end_sec + 0.01
        cues.append((start_sec, end_sec, line))

    return _render_srt(cues)


def _build_sectioned_srt(
    *,
    subtitle_body_text: str,
    segments: list[dict[str, Any]],
    tts_timing: dict[str, float],
) -> str:
    opening_lines = _extract_subtitle_lines(YOUTUBE_CAPTION_OPENING_LINE)
    body_lines = _extract_subtitle_lines(subtitle_body_text)
    ending_lines = _extract_subtitle_lines(YOUTUBE_CAPTION_ENDING_LINE)
    cues: list[tuple[float, float, str]] = []

    body_start_sec = float(tts_timing["body_start_sec"])
    body_end_sec = float(tts_timing["body_end_sec"])
    ending_start_sec = float(tts_timing["ending_start_sec"])
    final_duration_sec = float(tts_timing["final_duration_sec"])

    if opening_lines:
        cues.extend(_build_proportional_cues(opening_lines, start_sec=0.0, end_sec=body_start_sec))

    if body_lines:
        body_segments = _crop_segments_to_window(segments, start_sec=body_start_sec, end_sec=body_end_sec)
        cues.extend(
            _build_cues_from_lines_and_segments(
                body_lines,
                body_segments,
                section_start_sec=body_start_sec,
                section_end_sec=ending_start_sec,
            )
        )

    if ending_lines:
        cues.extend(_build_proportional_cues(ending_lines, start_sec=ending_start_sec, end_sec=final_duration_sec))

    if not cues:
        raise RuntimeError("sectioned subtitle cues are empty")
    return _render_srt(cues)


def _upload_youtube_caption(
    *,
    youtube: Any,
    job_id: str,
    video_id: str,
    subtitle_text: str,
    subtitle_body_text: str,
    audio_path: str,
) -> dict[str, Any]:
    if not _youtube_caption_enabled():
        return {
            "status": "skipped",
            "error_message": "",
            "request_json": {"enabled": False},
            "response_json": {},
        }

    subtitle_hash = _subtitle_sha256(subtitle_text)
    if not subtitle_text.strip():
        return {
            "status": "failed",
            "error_message": "subtitle_script_text is empty",
            "request_json": {
                "enabled": True,
                "subtitle_source": "subtitle_script_text+fixed_opening_ending",
                "line_count": 0,
                "original_line_count": 0,
                "caption_line_count": 0,
                "caption_has_opening": False,
                "caption_has_ending": False,
                "subtitle_sha256": subtitle_hash,
            },
            "response_json": {},
        }

    try:
        script_json = _load_job_script_json(job_id)
        spoken_body_text = str(script_json.get("tts_script_text") or subtitle_body_text or "").strip()
        selected_tts_timing = _normalize_tts_timing(script_json.get("selected_tts_timing"))
        artifacts = _build_caption_artifacts_for_audio_path(
            job_id=job_id,
            audio_path=audio_path,
            subtitle_body_text=subtitle_body_text,
            spoken_body_text=spoken_body_text,
            selected_tts_timing=selected_tts_timing,
        )
        _record_youtube_asr_cost_event(job_id=job_id, context_label="youtube_publish", artifacts=artifacts)
        caption_snippet = {
            "videoId": video_id,
            "language": _youtube_caption_language(),
            "name": _youtube_caption_name(),
            "isDraft": _youtube_caption_is_draft(),
        }

        srt_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".srt", mode="w", encoding="utf-8") as srt_file:
                srt_file.write(str(artifacts["srt_content"]))
                srt_path = srt_file.name

            media = MediaFileUpload(srt_path, mimetype="application/octet-stream", resumable=False)
            response = (
                youtube.captions()
                .insert(part="snippet", body={"snippet": caption_snippet}, media_body=media)
                .execute()
            )
            return {
                "status": "uploaded",
                "error_message": "",
                "request_json": {
                    "caption_snippet": caption_snippet,
                    **dict(artifacts["request_json"]),
                },
                "response_json": response if isinstance(response, dict) else {"raw": str(response)},
            }
        finally:
            if srt_path:
                try:
                    os.unlink(srt_path)
                except OSError:
                    logger.warning("[youtube] failed to remove temp srt: %s", srt_path)
    except Exception as e:
        return {
            "status": "failed",
            "error_message": f"input_source=audio_url; {e}",
            "request_json": {
                "enabled": True,
                "subtitle_source": "subtitle_script_text+fixed_opening_ending",
                "line_count": len(_split_alignment_sentences(subtitle_text)),
                "original_line_count": len([line.strip() for line in (subtitle_text or "").splitlines() if line.strip()]),
                "caption_line_count": len(_split_alignment_sentences(subtitle_text)),
                "caption_has_opening": bool(subtitle_text.strip().startswith(YOUTUBE_CAPTION_OPENING_LINE)),
                "caption_has_ending": bool(subtitle_text.strip().endswith(YOUTUBE_CAPTION_ENDING_LINE)),
                "subtitle_sha256": subtitle_hash,
                "input_source": "audio_url",
                "audio_size_bytes": int(os.path.getsize(audio_path)) if os.path.exists(audio_path) else 0,
                "timing_source": "whisper_word_timestamps",
                "alignment_status": "failed",
            },
            "response_json": {},
        }


def _youtube_credentials() -> Credentials:
    credentials = Credentials(
        token=None,
        refresh_token=_require_env("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_require_env("YOUTUBE_CLIENT_ID"),
        client_secret=_require_env("YOUTUBE_CLIENT_SECRET"),
        scopes=_youtube_scopes(),
    )
    credentials.refresh(GoogleAuthRequest())
    return credentials


def _publish_youtube(req: PublishRequest) -> dict:
    upload_request_body = {
        "snippet": {
            "title": _youtube_title(req),
            "description": _youtube_description(req),
            "categoryId": os.environ.get("YOUTUBE_CATEGORY_ID_DEFAULT", "28").strip() or "28",
        },
        "status": {
            "privacyStatus": os.environ.get("YOUTUBE_PRIVACY_STATUS_DEFAULT", "private").strip() or "private",
            "selfDeclaredMadeForKids": _env_bool("YOUTUBE_MADE_FOR_KIDS_DEFAULT", False),
        },
    }

    temp_video_path = ""
    temp_audio_path = ""
    try:
        audio_url = str(req.audio_url or "").strip()
        if not audio_url:
            raise RuntimeError("audio_url is required for youtube caption ASR")
        temp_audio_path = _download_audio_to_temp(audio_url)
        temp_video_path = _download_video_to_temp(req.video_url)
        youtube = build("youtube", "v3", credentials=_youtube_credentials(), cache_discovery=False)
        media = MediaFileUpload(temp_video_path, mimetype="video/mp4", resumable=True)
        upload = youtube.videos().insert(
            part="snippet,status",
            body=upload_request_body,
            media_body=media,
        )

        response = None
        while response is None:
            _, response = upload.next_chunk()

        video_id = str(response.get("id") or "").strip()
        if not video_id:
            raise RuntimeError(f"YouTube upload returned no id: {response}")

        caption_result = _upload_youtube_caption(
            youtube=youtube,
            job_id=req.job_id,
            video_id=video_id,
            subtitle_text=_youtube_subtitle_source(req),
            subtitle_body_text=req.subtitle_script_text,
            audio_path=temp_audio_path,
        )
        caption_status = str(caption_result.get("status") or "").strip()
        caption_error = str(caption_result.get("error_message") or "").strip()
        caption_warning = ""
        if caption_status != "uploaded" and _youtube_caption_enabled():
            caption_warning = f"caption upload failed: {caption_error or caption_status or 'unknown'}"

        if caption_warning and _youtube_caption_failure_blocks_publish():
            return {
                "status": "failed",
                "platform_post_id": video_id,
                "platform_post_url": f"https://www.youtube.com/watch?v={video_id}",
                "error_message": caption_warning,
                "request_json": {
                    "video_insert": upload_request_body,
                    "audio_url": audio_url,
                    "caption_insert": caption_result.get("request_json", {}),
                },
                "response_json": {
                    "video_insert": response if isinstance(response, dict) else {"raw": str(response)},
                    "caption_insert": caption_result.get("response_json", {}),
                    "caption_status": caption_result.get("status"),
                },
            }

        return {
            "status": "published",
            "platform_post_id": video_id,
            "platform_post_url": f"https://www.youtube.com/watch?v={video_id}",
            "error_message": caption_warning.strip(),
            "request_json": {
                "video_insert": upload_request_body,
                "audio_url": audio_url,
                "caption_insert": caption_result.get("request_json", {}),
            },
            "response_json": {
                "video_insert": response if isinstance(response, dict) else {"raw": str(response)},
                "caption_insert": caption_result.get("response_json", {}),
                "caption_status": caption_result.get("status"),
            },
        }
    except Exception as e:
        return {
            "status": "failed",
            "platform_post_id": "",
            "platform_post_url": "",
            "error_message": str(e),
            "request_json": {"video_insert": upload_request_body, "audio_url": str(req.audio_url or "").strip()},
            "response_json": {},
        }
    finally:
        if temp_video_path:
            try:
                os.unlink(temp_video_path)
            except OSError:
                logger.warning("[youtube] failed to remove temp video file: %s", temp_video_path)
        if temp_audio_path:
            try:
                os.unlink(temp_audio_path)
            except OSError:
                logger.warning("[youtube] failed to remove temp audio file: %s", temp_audio_path)


def _graph_version() -> str:
    return os.environ.get("INSTAGRAM_GRAPH_API_VERSION", "v23.0").strip() or "v23.0"


def _instagram_base_url() -> str:
    return f"{INSTAGRAM_GRAPH_API_BASE}/{_graph_version()}"


def _instagram_token() -> str:
    return _require_env("INSTAGRAM_PAGE_ACCESS_TOKEN")


def _instagram_ig_user_id() -> str:
    return _require_env("INSTAGRAM_IG_USER_ID")


def _publish_instagram(req: PublishRequest) -> dict:
    base_url = _instagram_base_url()
    access_token = _instagram_token()
    ig_user_id = _instagram_ig_user_id()
    share_to_feed = _env_bool("INSTAGRAM_SHARE_TO_FEED_DEFAULT", False)

    create_payload = {
        "media_type": "REELS",
        "video_url": req.video_url,
        "caption": _instagram_caption(req),
        "share_to_feed": str(share_to_feed).lower(),
        "access_token": access_token,
    }

    create_response_json: dict = {}
    status_response_json: dict = {}
    publish_response_json: dict = {}
    container_id = ""
    media_id = ""
    try:
        create_resp = requests.post(
            f"{base_url}/{ig_user_id}/media",
            data=create_payload,
            timeout=(15, 120),
        )
        create_resp.raise_for_status()
        create_response_json = create_resp.json()
        container_id = str(create_response_json.get("id") or "").strip()
        if not container_id:
            raise RuntimeError(f"Instagram container id missing: {create_response_json}")

        poll_interval = max(3, int(os.environ.get("INSTAGRAM_POLL_INTERVAL_SECONDS", "10")))
        max_wait = max(poll_interval, int(os.environ.get("INSTAGRAM_MAX_WAIT_SECONDS", "600")))
        deadline = time.time() + max_wait
        status_code = ""
        while time.time() < deadline:
            status_resp = requests.get(
                f"{base_url}/{container_id}",
                params={
                    "fields": "status_code,status",
                    "access_token": access_token,
                },
                timeout=(15, 60),
            )
            status_resp.raise_for_status()
            status_response_json = status_resp.json()
            status_code = str(status_response_json.get("status_code") or "").upper()
            if status_code == "FINISHED":
                break
            if status_code in {"ERROR", "EXPIRED"}:
                raise RuntimeError(f"Instagram container processing failed: {status_response_json}")
            time.sleep(poll_interval)
        else:
            raise RuntimeError(f"Instagram container processing timed out: {status_response_json}")

        publish_resp = requests.post(
            f"{base_url}/{ig_user_id}/media_publish",
            data={
                "creation_id": container_id,
                "access_token": access_token,
            },
            timeout=(15, 60),
        )
        publish_resp.raise_for_status()
        publish_response_json = publish_resp.json()
        media_id = str(publish_response_json.get("id") or "").strip()
        if not media_id:
            raise RuntimeError(f"Instagram media_publish returned no id: {publish_response_json}")

        permalink = ""
        try:
            permalink_resp = requests.get(
                f"{base_url}/{media_id}",
                params={
                    "fields": "permalink",
                    "access_token": access_token,
                },
                timeout=(15, 60),
            )
            permalink_resp.raise_for_status()
            permalink = str((permalink_resp.json() or {}).get("permalink") or "").strip()
        except Exception as permalink_error:
            logger.warning("[instagram] permalink lookup failed media_id=%s: %s", media_id, permalink_error)

        return {
            "status": "published",
            "platform_post_id": media_id,
            "platform_post_url": permalink,
            "error_message": "",
            "request_json": {
                "create": create_payload,
            },
            "response_json": {
                "create": create_response_json,
                "status": status_response_json,
                "publish": publish_response_json,
            },
        }
    except Exception as e:
        return {
            "status": "failed",
            "platform_post_id": media_id or container_id,
            "platform_post_url": "",
            "error_message": str(e),
            "request_json": {
                "create": create_payload,
            },
            "response_json": {
                "create": create_response_json,
                "status": status_response_json,
                "publish": publish_response_json,
            },
        }


def _normalize_targets(raw_targets: list[str]) -> list[str]:
    normalized: list[str] = []
    for target in raw_targets:
        value = str(target or "").strip().lower()
        if not value or value in normalized:
            continue
        if value not in ALLOWED_TARGETS:
            raise HTTPException(status_code=400, detail=f"unsupported target: {value}")
        normalized.append(value)
    if not normalized:
        raise HTTPException(status_code=400, detail="at least one target is required")
    return normalized


def _build_summary_text(job_id: str, results: dict[str, dict]) -> tuple[str, str]:
    success_targets = [platform for platform, result in results.items() if result.get("status") == "published"]
    failed_targets = [platform for platform, result in results.items() if result.get("status") != "published"]

    if success_targets and failed_targets:
        final_status = "PARTIALLY_PUBLISHED"
    elif success_targets:
        final_status = "PUBLISHED"
    else:
        final_status = "PUBLISH_FAILED"

    lines = ["✅ SNS 업로드 결과", "", f"Job ID: {job_id[:8]}"]
    for platform in ("youtube", "instagram"):
        if platform not in results:
            continue
        result = results[platform]
        label = "YouTube" if platform == "youtube" else "Instagram"
        if result.get("status") == "published":
            url = str(result.get("platform_post_url") or "").strip()
            line = f"• {label}: 성공" + (f" ({url})" if url else "")
            warning = str(result.get("error_message") or "").strip()
            if warning:
                line += f" [경고: {warning[:200]}]"
            lines.append(line)
        else:
            error = str(result.get("error_message") or "알 수 없는 오류").strip()
            lines.append(f"• {label}: 실패 - {error[:300]}")
    return final_status, "\n".join(lines)


def _publish_sync(body: PublishRequest) -> dict:
    targets = _normalize_targets(body.targets)
    results: dict[str, dict] = {}

    for platform in targets:
        readiness = _target_readiness(platform)
        if not readiness["ready"]:
            missing_env = readiness["missing_env"]
            result = {
                "status": "failed",
                "platform_post_id": "",
                "platform_post_url": "",
                "error_message": f"missing required env: {', '.join(missing_env)}",
                "request_json": {"target": platform},
                "response_json": {"missing_env": missing_env},
            }
            _persist_platform_result(body.job_id, platform, result)
            results[platform] = result
            logger.warning(
                "[publish] skip job_id=%s platform=%s reason=missing_env missing=%s",
                body.job_id,
                platform,
                ",".join(missing_env),
            )
            continue

        logger.info("[publish] start job_id=%s platform=%s", body.job_id, platform)
        if platform == "youtube":
            result = _publish_youtube(body)
        elif platform == "instagram":
            result = _publish_instagram(body)
        else:
            result = {
                "status": "failed",
                "platform_post_id": "",
                "platform_post_url": "",
                "error_message": f"unsupported target: {platform}",
                "request_json": {},
                "response_json": {},
            }
        _persist_platform_result(body.job_id, platform, result)
        results[platform] = result
        logger.info(
            "[publish] done job_id=%s platform=%s status=%s",
            body.job_id,
            platform,
            result.get("status", "failed"),
        )

    final_status, summary_text = _build_summary_text(body.job_id, results)
    return {
        "status": "completed",
        "job_id": body.job_id,
        "final_status": final_status,
        "summary_text": summary_text,
        "results": results,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_schema_sync()
    logger.info("sns-publisher-service started")
    yield
    logger.info("sns-publisher-service shutdown")


app = FastAPI(title="SNS Publisher Service", lifespan=lifespan)


@app.post("/publish")
async def publish(body: PublishRequest) -> dict:
    if not (body.video_url or "").strip():
        raise HTTPException(status_code=400, detail="video_url is required")
    try:
        return _publish_sync(body)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[publish] failed job_id=%s", body.job_id)
        raise HTTPException(status_code=500, detail=f"publish failed: {e}") from e


@app.post("/internal/caption-artifacts")
async def caption_artifacts(body: CaptionArtifactRequest) -> dict:
    if not (body.audio_url or "").strip():
        raise HTTPException(status_code=400, detail="audio_url is required")
    if not (body.subtitle_script_text or "").strip():
        raise HTTPException(status_code=400, detail="subtitle_script_text is required")
    if not (body.tts_script_text or "").strip():
        raise HTTPException(status_code=400, detail="tts_script_text is required")

    audio_path = ""
    try:
        audio_path = _download_audio_to_temp(body.audio_url)
        artifacts = _build_caption_artifacts_for_audio_path(
            job_id=body.job_id,
            audio_path=audio_path,
            subtitle_body_text=body.subtitle_script_text,
            spoken_body_text=body.tts_script_text,
            selected_tts_timing=body.selected_tts_timing,
        )
        _record_youtube_asr_cost_event(job_id=body.job_id, context_label="caption_artifacts", artifacts=artifacts)
        return {
            "status": "ready",
            "job_id": body.job_id,
            "subtitle_text": artifacts["subtitle_text"],
            "spoken_text": artifacts["spoken_text"],
            "srt_content": artifacts["srt_content"],
            "asr_model": artifacts["asr_model"],
            "word_count": artifacts["word_count"],
            "segment_count": artifacts["segment_count"],
            "spoken_sentence_count": artifacts["spoken_sentence_count"],
            "display_sentence_count": artifacts["display_sentence_count"],
            "matched_sentence_count": artifacts["matched_sentence_count"],
            "alignment_status": artifacts["alignment_status"],
            "fallback_reason": artifacts["fallback_reason"],
            "timing_source": artifacts["timing_source"],
            "cue_count": artifacts["cue_count"],
            "subtitle_sha256": artifacts["subtitle_sha256"],
            "request_json": artifacts["request_json"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[caption-artifacts] failed job_id=%s", body.job_id)
        raise HTTPException(status_code=500, detail=f"caption artifacts failed: {e}") from e
    finally:
        if audio_path:
            try:
                os.unlink(audio_path)
            except OSError:
                logger.warning("[caption-artifacts] failed to remove temp audio: %s", audio_path)


@app.get("/health")
async def health() -> dict:
    readiness = _all_target_readiness()
    return {
        "status": "ok",
        "targets": sorted(ALLOWED_TARGETS),
        "readiness": readiness,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)
