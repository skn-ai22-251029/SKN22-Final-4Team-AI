import base64
import os
from typing import Any

import requests
import runpod


TTS_LOCAL_API_BASE = os.getenv("TTS_LOCAL_API_BASE", "http://127.0.0.1:9880").rstrip("/")
TTS_LOCAL_TIMEOUT_SECONDS = float(os.getenv("TTS_LOCAL_TIMEOUT_SECONDS", "300"))


def _build_local_endpoint() -> str:
    return f"{TTS_LOCAL_API_BASE}/tts"


def _normalize_input(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("input") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("input must be an object")
    if not str(payload.get("text") or "").strip():
        raise ValueError("input.text is required")
    return payload


def _call_local_tts(payload: dict[str, Any]) -> bytes:
    endpoint = _build_local_endpoint()
    resp = requests.post(
        endpoint,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=TTS_LOCAL_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        detail = (resp.text or "")[:800]
        raise RuntimeError(f"local tts failed: {resp.status_code} {detail}")
    if not resp.content:
        raise RuntimeError("local tts returned empty audio body")
    return resp.content


def handler(event: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = _normalize_input(event)
        audio_bytes = _call_local_tts(payload)
        return {
            "ok": True,
            "content_type": "audio/wav",
            "audio_base64": base64.b64encode(audio_bytes).decode("utf-8"),
            "bytes": len(audio_bytes),
        }
    except Exception as e:
        # Serverless status는 COMPLETED일 수 있으므로
        # 라우터가 output.ok/error를 보고 failed 처리한다.
        return {
            "ok": False,
            "error": str(e),
        }


runpod.serverless.start({"handler": handler})
