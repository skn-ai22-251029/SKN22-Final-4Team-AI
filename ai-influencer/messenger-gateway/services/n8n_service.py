import asyncio
import logging
from urllib.parse import urlparse, urlunparse
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None

_WEBHOOK_PATH_TO_WORKFLOW_ID = {
    "wf-01-input": "Mt5nwvystMhfO1nl",
    "wf-05-confirm": "gD9A0qy9MxY8g0T6",
    "wf-06-report": "QSrXdaRpKosyZIj3",
    "wf-08-sns-upload": "uLRW8JT5UitrhCC9",
    "wf-11-tts-generate": "Wv5SdSdlPLwNzeqF",
    "wf-12-heygen-generate-v2": "WF12HeygenV2Run",
}


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("n8n http client closed")


def _headers() -> dict[str, str]:
    return {"X-Internal-Secret": settings.gateway_internal_secret}


def _build_webhook_fallback_url(url: str) -> Optional[str]:
    """Legacy /webhook/<path> URL을 /webhook/<workflowId>/webhook/<path>로 변환한다."""
    parsed = urlparse(url)
    prefix = "/webhook/"
    if not parsed.path.startswith(prefix):
        return None
    tail = parsed.path[len(prefix):].strip("/")
    # 이미 /webhook/<id>/webhook/<path> 형태이면 fallback 불필요
    if "/webhook/" in tail:
        return None
    workflow_id = _WEBHOOK_PATH_TO_WORKFLOW_ID.get(tail)
    if not workflow_id:
        return None
    fallback_path = f"/webhook/{workflow_id}/webhook/{tail}"
    if fallback_path == parsed.path:
        return None
    return urlunparse(parsed._replace(path=fallback_path))


async def _post_with_retry(url: str, payload: dict, max_retries: int = 3) -> httpx.Response:
    client = get_http_client()
    candidates: list[str] = [url]
    fallback_url = _build_webhook_fallback_url(url)
    if fallback_url and fallback_url not in candidates:
        candidates.append(fallback_url)

    last_error: Exception | None = None
    for candidate_index, candidate_url in enumerate(candidates):
        for attempt in range(max_retries):
            try:
                resp = await client.post(candidate_url, json=payload, headers=_headers())
                resp.raise_for_status()
                if candidate_index > 0:
                    logger.info("n8n call fallback succeeded url=%s", candidate_url)
                return resp
            except httpx.HTTPStatusError as e:
                last_error = e
                status_code = e.response.status_code if e.response is not None else 0
                # legacy URL이 404면 ID URL 후보를 즉시 시도한다.
                if status_code == 404 and candidate_index < len(candidates) - 1:
                    logger.warning(
                        "n8n webhook not found (url=%s status=404), trying fallback url=%s",
                        candidate_url,
                        candidates[candidate_index + 1],
                    )
                    break
                if attempt == max_retries - 1:
                    logger.error("n8n call failed url=%s status=%s", candidate_url, status_code)
                    break
                wait = 2 ** attempt
                logger.warning(
                    "n8n call failed (attempt %d/%d) url=%s status=%s, retrying in %ds",
                    attempt + 1,
                    max_retries,
                    candidate_url,
                    status_code,
                    wait,
                )
                await asyncio.sleep(wait)
            except httpx.RequestError as e:
                last_error = e
                if attempt == max_retries - 1:
                    logger.error("n8n request error url=%s: %s", candidate_url, e)
                    break
                wait = 2 ** attempt
                logger.warning(
                    "n8n request error (attempt %d/%d) url=%s, retrying in %ds: %s",
                    attempt + 1,
                    max_retries,
                    candidate_url,
                    wait,
                    e,
                )
                await asyncio.sleep(wait)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"n8n call failed without explicit error: url={url}")


async def call_wf01_input(
    job_id: str,
    messenger_source: str,
    messenger_user_id: str,
    messenger_channel_id: str,
    concept_text: str,
    ref_image_url: Optional[str],
    character_id: str,
) -> None:
    payload = {
        "job_id": job_id,
        "messenger_source": messenger_source,
        "messenger_user_id": messenger_user_id,
        "messenger_channel_id": messenger_channel_id,
        "concept_text": concept_text,
        "ref_image_url": ref_image_url or "",
        "character_id": character_id,
    }
    await _post_with_retry(settings.n8n_wf01_webhook_url, payload)
    logger.info("[%s] call_wf01_input job_id=%s", messenger_source, job_id)


async def call_wf05_confirm(
    job_id: str,
    action: str,
    revision_note: Optional[str] = None,
) -> None:
    payload = {
        "job_id": job_id,
        "action": action,
        "revision_note": revision_note or "",
    }
    await _post_with_retry(settings.n8n_wf05_webhook_url, payload)
    logger.info("call_wf05_confirm job_id=%s action=%s", job_id, action)


async def call_wf11_tts_generate(
    job_id: str,
    script_text: str,
    channel_id: str,
    user_id: str,
    auto_trigger_wf12: bool = False,
) -> None:
    payload = {
        "job_id": job_id,
        "script_text": script_text,
        "channel_id": channel_id,
        "user_id": user_id,
        "auto_trigger_wf12": auto_trigger_wf12,
    }
    await _post_with_retry(settings.n8n_wf11_webhook_url, payload)
    logger.info("call_wf11_tts_generate job_id=%s", job_id)


async def call_wf12_heygen_generate(
    job_id: str,
    channel_id: str,
    user_id: str,
    audio_url: str = "",
    avatar_id: str = "",
    use_avatar_iv_model: bool = False,
) -> None:
    payload = {
        "job_id": job_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "audio_url": audio_url,
        "avatar_id": avatar_id,
        "use_avatar_iv_model": use_avatar_iv_model,
    }
    await _post_with_retry(settings.n8n_wf12_webhook_url, payload)
    logger.info("call_wf12_heygen_generate job_id=%s", job_id)


async def call_wf08_sns_upload(
    job_id: str,
    video_url: str,
    audio_url: str,
    channel_id: str,
    targets: list[str],
    video_filename: str = "",
    title: str = "",
    description: str = "",
    caption: str = "",
    subtitle_script_text: str = "",
) -> None:
    if not video_url:
        raise ValueError("video_url is required for WF-08 upload")
    if not audio_url:
        raise ValueError("audio_url is required for WF-08 upload")
    if not targets:
        raise ValueError("targets are required for WF-08 upload")
    payload = {
        "job_id": job_id,
        "video_url": video_url,
        "audio_url": audio_url,
        "channel_id": channel_id,
        "targets": targets,
        "video_filename": video_filename,
        "title": title,
        "description": description,
        "caption": caption,
        "subtitle_script_text": subtitle_script_text,
    }
    await _post_with_retry(settings.n8n_wf08_webhook_url, payload)
    logger.info("call_wf08_sns_upload job_id=%s targets=%s video_filename=%s", job_id, ",".join(targets), video_filename)


async def call_wf06_report(
    job_id: str,
    messenger_source: str,
    messenger_user_id: str,
    messenger_channel_id: str,
    prompt: str,
    notebook_id: str,
    channel_id: str,
    character_id: str,
) -> None:
    payload = {
        "job_id": job_id,
        "messenger_source": messenger_source,
        "messenger_user_id": messenger_user_id,
        "messenger_channel_id": messenger_channel_id,
        "prompt": prompt,
        "notebook_id": notebook_id,
        "channel_id": channel_id,
        "character_id": character_id,
    }
    await _post_with_retry(settings.n8n_wf06_webhook_url, payload)
    logger.info("[%s] call_wf06_report job_id=%s", messenger_source, job_id)
