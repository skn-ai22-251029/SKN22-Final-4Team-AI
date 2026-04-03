import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager

import psycopg2
import requests
from fastapi import FastAPI, HTTPException
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from pydantic import BaseModel, Field
from psycopg2.extras import Json


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
INSTAGRAM_GRAPH_API_BASE = "https://graph.facebook.com"
ALLOWED_TARGETS = {"youtube", "instagram"}


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


def _youtube_credentials() -> Credentials:
    credentials = Credentials(
        token=None,
        refresh_token=_require_env("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_require_env("YOUTUBE_CLIENT_ID"),
        client_secret=_require_env("YOUTUBE_CLIENT_SECRET"),
        scopes=[YOUTUBE_SCOPE],
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
        return {
            "status": "published",
            "platform_post_id": video_id,
            "platform_post_url": f"https://www.youtube.com/watch?v={video_id}",
            "error_message": "",
            "request_json": upload_request_body,
            "response_json": response,
        }
    except Exception as e:
        return {
            "status": "failed",
            "platform_post_id": "",
            "platform_post_url": "",
            "error_message": str(e),
            "request_json": upload_request_body,
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
            lines.append(f"• {label}: 성공" + (f" ({url})" if url else ""))
        else:
            error = str(result.get("error_message") or "알 수 없는 오류").strip()
            lines.append(f"• {label}: 실패 - {error[:300]}")
    return final_status, "\n".join(lines)


def _publish_sync(body: PublishRequest) -> dict:
    targets = _normalize_targets(body.targets)
    results: dict[str, dict] = {}

    for platform in targets:
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
    return {"status": "ok", "targets": sorted(ALLOWED_TARGETS)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)
