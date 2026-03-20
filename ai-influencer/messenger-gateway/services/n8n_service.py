import asyncio
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


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


async def _post_with_retry(url: str, payload: dict, max_retries: int = 3) -> httpx.Response:
    client = get_http_client()
    for attempt in range(max_retries):
        try:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            logger.warning("n8n call failed (attempt %d/%d), retrying in %ds: %s", attempt + 1, max_retries, wait, e)
            await asyncio.sleep(wait)


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


async def call_wf07_tts_heygen(
    job_id: str,
    script_text: str,
    channel_id: str,
    user_id: str,
) -> None:
    payload = {
        "job_id": job_id,
        "script_text": script_text,
        "channel_id": channel_id,
        "user_id": user_id,
    }
    await _post_with_retry(settings.n8n_wf07_webhook_url, payload)
    logger.info("call_wf07_tts_heygen job_id=%s", job_id)


async def call_wf08_sns_upload(
    job_id: str,
    video_url: str,
    channel_id: str,
) -> None:
    payload = {
        "job_id": job_id,
        "video_url": video_url,
        "channel_id": channel_id,
    }
    await _post_with_retry(settings.n8n_wf08_webhook_url, payload)
    logger.info("call_wf08_sns_upload job_id=%s", job_id)


async def call_wf06_report(
    job_id: str,
    messenger_source: str,
    messenger_user_id: str,
    messenger_channel_id: str,
    prompt: str,
    notebook_id: str,
    character_id: str,
) -> None:
    payload = {
        "job_id": job_id,
        "messenger_source": messenger_source,
        "messenger_user_id": messenger_user_id,
        "messenger_channel_id": messenger_channel_id,
        "prompt": prompt,
        "notebook_id": notebook_id,
        "character_id": character_id,
    }
    client = get_http_client()
    resp = await client.post(settings.n8n_wf06_webhook_url, json=payload, headers=_headers())
    resp.raise_for_status()
    logger.info("[%s] call_wf06_report job_id=%s", messenger_source, job_id)
