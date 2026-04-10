import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

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
    targets: list[str] = Field(default_factory=list)
    title: str = ""
    description: str = ""
    caption: str = ""
    video_filename: str = ""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


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


def _download_video_to_temp(video_url: str) -> str:
    suffix = ".mp4"
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        with requests.get(video_url, stream=True, timeout=(15, 300)) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    temp_file.write(chunk)
        temp_file.flush()
        return temp_file.name
    finally:
        temp_file.close()


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


def _asr_primary_model() -> str:
    return os.environ.get("YOUTUBE_CAPTION_ASR_MODEL", "gpt-4o-transcribe").strip() or "gpt-4o-transcribe"


def _openai_client() -> OpenAI:
    return OpenAI(api_key=_require_env("OPENAI_API_KEY"))


def _extract_subtitle_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


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


def _transcribe_segments(media_path: str, language: str) -> tuple[list[dict[str, Any]], str]:
    client = _openai_client()
    model_candidates = [_asr_primary_model(), "whisper-1"]
    seen: set[str] = set()
    last_error = ""

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
                    timestamp_granularities=["segment"],
                )
            segments = _normalize_segments(getattr(response, "segments", None))
            if not segments and hasattr(response, "model_dump"):
                dumped = response.model_dump()
                segments = _normalize_segments(dumped.get("segments"))
            if not segments and isinstance(response, dict):
                segments = _normalize_segments(response.get("segments"))
            if segments:
                return segments, model_name
            last_error = f"ASR model={model_name} returned no segments"
        except Exception as e:
            last_error = f"ASR model={model_name} failed: {e}"
    raise RuntimeError(last_error or "ASR transcription failed")


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

    rows: list[str] = []
    for idx, (start_sec, end_sec, text) in enumerate(cues, start=1):
        rows.append(str(idx))
        rows.append(f"{_format_srt_timestamp(start_sec)} --> {_format_srt_timestamp(end_sec)}")
        rows.append(text)
        rows.append("")
    return "\n".join(rows).strip() + "\n"


def _upload_youtube_caption(
    *,
    youtube: Any,
    video_id: str,
    subtitle_text: str,
    media_path: str,
) -> dict[str, Any]:
    if not _youtube_caption_enabled():
        return {
            "status": "skipped",
            "error_message": "",
            "request_json": {"enabled": False},
            "response_json": {},
        }

    subtitle_lines = _extract_subtitle_lines(subtitle_text)
    if not subtitle_lines:
        return {
            "status": "skipped",
            "error_message": "subtitle text is empty",
            "request_json": {"enabled": True, "line_count": 0},
            "response_json": {},
        }

    try:
        language = _youtube_caption_language()
        segments, asr_model = _transcribe_segments(media_path, language)
        srt_content = _build_srt_from_subtitle_lines(subtitle_lines, segments)
        caption_snippet = {
            "videoId": video_id,
            "language": language,
            "name": _youtube_caption_name(),
            "isDraft": _youtube_caption_is_draft(),
        }

        srt_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".srt", mode="w", encoding="utf-8") as srt_file:
                srt_file.write(srt_content)
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
                    "enabled": True,
                    "caption_snippet": caption_snippet,
                    "line_count": len(subtitle_lines),
                    "asr_model": asr_model,
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
            "error_message": str(e),
            "request_json": {
                "enabled": True,
                "line_count": len(subtitle_lines),
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

    temp_path = ""
    try:
        temp_path = _download_video_to_temp(req.video_url)
        youtube = build("youtube", "v3", credentials=_youtube_credentials(), cache_discovery=False)
        media = MediaFileUpload(temp_path, mimetype="video/mp4", resumable=True)
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
            video_id=video_id,
            subtitle_text=_youtube_description(req),
            media_path=temp_path,
        )
        caption_warning = ""
        if caption_result.get("status") == "failed":
            caption_warning = f"caption upload failed: {caption_result.get('error_message', '')}"

        return {
            "status": "published",
            "platform_post_id": video_id,
            "platform_post_url": f"https://www.youtube.com/watch?v={video_id}",
            "error_message": caption_warning.strip(),
            "request_json": {
                "video_insert": upload_request_body,
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
            "request_json": {"video_insert": upload_request_body},
            "response_json": {},
        }
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                logger.warning("[youtube] failed to remove temp file: %s", temp_path)


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
