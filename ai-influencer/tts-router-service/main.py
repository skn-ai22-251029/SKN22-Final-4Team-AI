import asyncio
import base64
import binascii
import json
import logging
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic_settings import BaseSettings


logger = logging.getLogger("tts-router")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class Settings(BaseSettings):
    tts_router_mode: str = "legacy_http"  # legacy_http | runpod_serverless
    tts_router_failopen_to_legacy: bool = True

    # Legacy path (current Cloudflare/Pod endpoint)
    tts_public_base_url: str = ""
    tts_legacy_api_url: str = ""
    tts_request_timeout_seconds: float = 300.0

    # RunPod Serverless path
    runpod_api_key: str = ""
    runpod_serverless_endpoint_id: str = ""
    runpod_api_base_url: str = "https://api.runpod.ai/v2"
    runpod_poll_interval_seconds: float = 2.0
    runpod_max_wait_seconds: float = 300.0

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
app = FastAPI(title="tts-router-service")


def _normalize_mode(raw: str) -> str:
    mode = (raw or "").strip().lower()
    return mode if mode in {"legacy_http", "runpod_serverless"} else "legacy_http"


def _legacy_tts_endpoint() -> str:
    base = settings.tts_public_base_url.strip() or settings.tts_legacy_api_url.strip()
    if not base:
        return ""
    return base if base.rstrip("/").endswith("/tts") else f"{base.rstrip('/')}/tts"


def _http_timeout() -> httpx.Timeout:
    timeout = max(5.0, float(settings.tts_request_timeout_seconds))
    return httpx.Timeout(timeout=timeout)


def _clip(text: str, limit: int = 700) -> str:
    value = (text or "").strip()
    return value if len(value) <= limit else value[:limit]


def _format_runpod_error(
    message: str,
    status_code: int = 502,
    *,
    endpoint_id: str = "",
    job_id: str = "",
    status_text: str = "",
) -> PlainTextResponse:
    ctx: list[str] = []
    if endpoint_id:
        ctx.append(f"endpoint={endpoint_id}")
    if job_id:
        ctx.append(f"job_id={job_id}")
    if status_text:
        ctx.append(f"status={status_text}")
    ctx_prefix = f"[{', '.join(ctx)}] " if ctx else ""
    return PlainTextResponse(
        status_code=status_code,
        content=f"TTS API failed [tts_server_runtime_error]: {ctx_prefix}{message}",
    )


def _decode_audio_base64(value: str) -> bytes:
    return base64.b64decode(value.encode("utf-8"), validate=True)


def _extract_audio_bytes_from_output(output: Any) -> bytes:
    candidates: list[Any] = []

    def add_candidate(v: Any) -> None:
        if v is not None:
            candidates.append(v)

    add_candidate(output)
    if isinstance(output, dict):
        add_candidate(output.get("audio_base64"))
        add_candidate(output.get("audio_content_b64"))
        add_candidate(output.get("audio_b64"))
        add_candidate(output.get("wav_base64"))
        add_candidate(output.get("audio"))
        data_obj = output.get("data")
        if isinstance(data_obj, dict):
            add_candidate(data_obj.get("audio_base64"))
            add_candidate(data_obj.get("audio_content_b64"))
            add_candidate(data_obj.get("audio"))
    if isinstance(output, list) and output:
        add_candidate(output[0])

    for item in candidates:
        if isinstance(item, (bytes, bytearray)):
            return bytes(item)
        if isinstance(item, str) and item.strip():
            token = item.strip()
            try:
                return _decode_audio_base64(token)
            except (binascii.Error, ValueError):
                continue
        if isinstance(item, dict):
            nested = _extract_audio_bytes_from_output(item)
            if nested:
                return nested
    return b""


async def _call_legacy_tts(payload: dict[str, Any]) -> Response:
    endpoint = _legacy_tts_endpoint()
    if not endpoint:
        return PlainTextResponse(
            status_code=500,
            content="TTS API failed [tts_server_runtime_error]: TTS_LEGACY_API_URL is not configured",
        )

    try:
        async with httpx.AsyncClient(timeout=_http_timeout()) as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as e:
        return PlainTextResponse(
            status_code=502,
            content=f"TTS API failed [tts_network_error]: {type(e).__name__}: {e}",
        )

    if not resp.is_success:
        return PlainTextResponse(status_code=resp.status_code, content=resp.text or "")
    content_type = resp.headers.get("content-type", "audio/wav")
    return Response(content=resp.content, media_type=content_type)


def _runpod_headers() -> dict[str, str]:
    api_key = settings.runpod_api_key.strip()
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_runpod_job_id(payload: Any) -> str:
    if isinstance(payload, dict):
        direct = str(payload.get("id") or "").strip()
        if direct:
            return direct
        data = payload.get("data")
        if isinstance(data, dict):
            nested = str(data.get("id") or "").strip()
            if nested:
                return nested
    return ""


def _extract_runpod_status(payload: Any) -> str:
    if isinstance(payload, dict):
        direct = str(payload.get("status") or "").strip()
        if direct:
            return direct.upper()
        data = payload.get("data")
        if isinstance(data, dict):
            nested = str(data.get("status") or "").strip()
            if nested:
                return nested.upper()
    return ""


def _extract_runpod_output(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "output" in payload:
            return payload.get("output")
        data = payload.get("data")
        if isinstance(data, dict) and "output" in data:
            return data.get("output")
    return None


def _extract_runpod_error(payload: Any) -> str:
    if isinstance(payload, dict):
        direct = payload.get("error")
        if direct:
            return _clip(str(direct))
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("error")
            if nested:
                if isinstance(nested, str):
                    return _clip(nested)
                try:
                    return _clip(json.dumps(nested, ensure_ascii=False))
                except Exception:
                    return _clip(str(nested))
    return ""


def _extract_runpod_output_error(output: Any) -> str:
    if isinstance(output, dict):
        ok = output.get("ok")
        if ok is False:
            detail = output.get("error") or output.get("message")
            if detail:
                return _clip(str(detail))
        nested = output.get("data")
        if isinstance(nested, dict):
            nested_ok = nested.get("ok")
            if nested_ok is False:
                detail = nested.get("error") or nested.get("message")
                if detail:
                    return _clip(str(detail))
    return ""


async def _call_runpod_serverless(payload: dict[str, Any]) -> Response:
    endpoint_id = settings.runpod_serverless_endpoint_id.strip()
    api_key = settings.runpod_api_key.strip()
    base = settings.runpod_api_base_url.strip().rstrip("/")

    if not endpoint_id or not api_key:
        return _format_runpod_error(
            "RUNPOD_SERVERLESS_ENDPOINT_ID or RUNPOD_API_KEY is not configured",
            500,
        )

    run_url = f"{base}/{endpoint_id}/run"
    status_url_prefix = f"{base}/{endpoint_id}/status"

    try:
        async with httpx.AsyncClient(timeout=_http_timeout()) as client:
            run_resp = await client.post(run_url, headers=_runpod_headers(), json={"input": payload})
            if not run_resp.is_success:
                return _format_runpod_error(
                    f"RunPod run failed: {run_resp.status_code} {_clip(run_resp.text)}",
                    run_resp.status_code,
                    endpoint_id=endpoint_id,
                )

            run_json = run_resp.json()
            run_output = _extract_runpod_output(run_json)
            if run_output is not None:
                output_error = _extract_runpod_output_error(run_output)
                if output_error:
                    return _format_runpod_error(
                        f"RunPod worker error: {output_error}",
                        endpoint_id=endpoint_id,
                    )
                audio = _extract_audio_bytes_from_output(run_output)
                if audio:
                    return Response(content=audio, media_type="audio/wav")

            job_id = _extract_runpod_job_id(run_json)
            if not job_id:
                return _format_runpod_error(
                    f"RunPod run response missing job id: {_clip(run_resp.text)}",
                    endpoint_id=endpoint_id,
                )

            poll_interval = max(0.5, float(settings.runpod_poll_interval_seconds))
            max_wait = max(poll_interval, float(settings.runpod_max_wait_seconds))
            deadline = asyncio.get_running_loop().time() + max_wait
            last_status = "IN_QUEUE"

            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(poll_interval)
                st_resp = await client.get(f"{status_url_prefix}/{job_id}", headers=_runpod_headers())
                if not st_resp.is_success:
                    return _format_runpod_error(
                        f"RunPod status failed: {st_resp.status_code} {_clip(st_resp.text)}",
                        st_resp.status_code,
                        endpoint_id=endpoint_id,
                        job_id=job_id,
                    )

                st_json = st_resp.json()
                status = _extract_runpod_status(st_json)
                if status:
                    last_status = status

                if status in {"COMPLETED", "SUCCESS"}:
                    output = _extract_runpod_output(st_json)
                    output_error = _extract_runpod_output_error(output)
                    if output_error:
                        return _format_runpod_error(
                            f"RunPod worker error: {output_error}",
                            endpoint_id=endpoint_id,
                            job_id=job_id,
                            status_text=status,
                        )
                    audio = _extract_audio_bytes_from_output(output)
                    if audio:
                        return Response(content=audio, media_type="audio/wav")
                    return _format_runpod_error(
                        f"RunPod completed but no audio output: {_clip(st_resp.text)}",
                        endpoint_id=endpoint_id,
                        job_id=job_id,
                        status_text=status,
                    )

                if status in {"FAILED", "CANCELLED", "TIMED_OUT", "TIMEOUT"}:
                    reason = _extract_runpod_error(st_json) or _clip(st_resp.text)
                    return _format_runpod_error(
                        f"RunPod job failed: {reason}",
                        endpoint_id=endpoint_id,
                        job_id=job_id,
                        status_text=status,
                    )

                if status not in {"IN_QUEUE", "IN_PROGRESS", "PROCESSING"}:
                    logger.warning("unexpected runpod status job_id=%s status=%s", job_id, status)

            return _format_runpod_error(
                "RunPod polling timeout",
                504,
                endpoint_id=endpoint_id,
                job_id=job_id,
                status_text=last_status,
            )
    except httpx.RequestError as e:
        return PlainTextResponse(
            status_code=502,
            content=f"TTS API failed [tts_network_error]: {type(e).__name__}: {e}",
        )
    except Exception as e:
        return _format_runpod_error(
            f"RunPod route exception: {type(e).__name__}: {e}",
            endpoint_id=endpoint_id,
        )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    mode = _normalize_mode(settings.tts_router_mode)
    legacy = bool(_legacy_tts_endpoint())
    serverless = bool(settings.runpod_serverless_endpoint_id.strip() and settings.runpod_api_key.strip())
    return JSONResponse(
        {
            "ok": True,
            "mode": mode,
            "legacy_configured": legacy,
            "runpod_configured": serverless,
            "failopen_to_legacy": bool(settings.tts_router_failopen_to_legacy),
        }
    )


@app.post("/tts")
async def tts_proxy(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return PlainTextResponse(status_code=400, content="invalid json body")
    if not isinstance(payload, dict):
        return PlainTextResponse(status_code=400, content="json object body required")

    mode = _normalize_mode(settings.tts_router_mode)
    if mode == "runpod_serverless":
        response = await _call_runpod_serverless(payload)
        if response.status_code < 400:
            return response
        if settings.tts_router_failopen_to_legacy:
            logger.warning("runpod failed, fallback to legacy_http status=%s", response.status_code)
            fallback = await _call_legacy_tts(payload)
            if fallback.status_code < 400:
                fallback.headers["x-tts-router-fallback"] = "legacy_http"
                return fallback
        return response

    return await _call_legacy_tts(payload)
