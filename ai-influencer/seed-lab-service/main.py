from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import importlib.util
import json
import logging
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import boto3
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings

sys.path.insert(0, str((Path(__file__).resolve().parent.parent / "scripts").resolve()))

from seed_lab import (  # type: ignore
    DEFAULT_AUTO_EVAL_ASR_MODEL,
    DEFAULT_AUTO_EVAL_JUDGE_MODEL,
    DEFAULT_AUTO_EVAL_PROFILE,
    DEFAULT_TIMEOUT,
    HUMAN_EVAL_JSON,
    LIVE_AUTO_EVAL_JSON,
    LIVE_RECORDS_JSONL,
    _auto_eval_single_record,
    _append_jsonl_object,
    _build_run_id,
    _expand_seed_list_with_random,
    _generate_review_html,
    _load_human_eval_map,
    _load_manifest,
    _load_seeds_file,
    _load_eval_payload,
    _merge_eval_maps,
    _normalize_record_for_ui,
    _parse_seed_values,
    _pick_scripts_for_stage,
    _read_jsonl_objects,
    _resolve_api_endpoint,
    _resolve_asr_model_for_transcription,
    _resolve_openai_keys,
    _resolve_serve_default_tts_params,
    _seedlab_runtime_capabilities,
    _to_bool,
    _to_float,
    _to_int,
    _upsert_eval_entry,
    _worker_generate_one,
    _write_human_eval_map,
    _write_manifest,
    load_dataset,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("seed-lab-service")


class Settings(BaseSettings):
    seedlab_port: int = 8300
    gateway_internal_secret: str
    seedlab_gateway_url: str = "http://messenger-gateway:8080"
    seedlab_run_root: str = "/app/runtime/seed-lab/runs"
    seedlab_default_dataset: str = "/app/scripts/seed_lab_dataset.local.json"
    seedlab_queue_concurrency: int = 1
    seedlab_sample_concurrency: int = 2
    tts_router_url: str = "http://tts-router-service:8300"
    tts_api_url: str = ""
    openai_fallback_api_key: str = ""
    openai_api_key_seedlab_asr: str = ""
    openai_api_key_seedlab_judge: str = ""
    seedlab_asr_model: str = DEFAULT_AUTO_EVAL_ASR_MODEL
    seedlab_judge_model: str = DEFAULT_AUTO_EVAL_JUDGE_MODEL
    seedlab_auto_eval_timeout: int = 120
    seedlab_evaluation_profile: str = DEFAULT_AUTO_EVAL_PROFILE
    seedlab_reference_audio_local_path: str = ""
    seedlab_reference_audio_s3_uri: str = ""
    seedlab_reference_audio_cache_dir: str = ""
    seedlab_disable_llm_note: bool = False
    seedlab_language: str = "ko"
    seedlab_eval_mode: str = "runpod_pod"
    seedlab_eval_runpod_url: str = ""
    seedlab_eval_runpod_shared_secret: str = ""
    seedlab_eval_runpod_timeout: int = 180
    seedlab_eval_runpod_poll_interval: float = 2.0
    seedlab_eval_runpod_preflight_timeout: float = 5.0
    seedlab_eval_runpod_http_timeout: float = 20.0
    seedlab_sample_s3_bucket: str = ""
    seedlab_sample_s3_prefix: str = "seed-lab-samples"
    seedlab_sample_s3_region: str = "ap-northeast-2"
    media_s3_bucket: str = ""
    seedlab_asr_cost_usd_per_minute: float = 0.0
    seedlab_judge_input_cost_usd_per_1m: float = 0.0
    seedlab_judge_output_cost_usd_per_1m: float = 0.0

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()


class RunCreateRequest(BaseModel):
    run_id: str = ""
    seeds: str = ""
    dup: bool = False
    dataset_path: str = ""


_s3_client: Any | None = None
_state_progress_lock = threading.Lock()


class RunpodEvalError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        detail: str,
        *,
        endpoint: str = "",
        remote_job_id: str = "",
        health_snapshot: Optional[dict[str, Any]] = None,
    ) -> None:
        self.error_code = str(error_code or "").strip() or "runpod_eval_error"
        self.detail = str(detail or "").strip() or "unknown error"
        self.endpoint = str(endpoint or "").strip()
        self.remote_job_id = str(remote_job_id or "").strip()
        self.health_snapshot = health_snapshot if isinstance(health_snapshot, dict) else {}
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        parts = [self.error_code, self.detail]
        if self.remote_job_id:
            parts.append(f"job_id={self.remote_job_id}")
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        summary = _runpod_health_summary_text(self.health_snapshot)
        if summary:
            parts.append(f"health={summary}")
        return " | ".join(parts)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _run_root() -> Path:
    root = Path(settings.seedlab_run_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_run_root_writable() -> tuple[Path, bool, str]:
    root = Path(settings.seedlab_run_root).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".seedlab-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return root, True, ""
    except Exception as e:
        return root, False, str(e)


def _run_dir(run_id: str) -> Path:
    return (_run_root() / run_id).resolve()


def _state_path(run_id: str) -> Path:
    return _run_dir(run_id) / "run_state.json"


def _live_eval_debug_path(run_id: str) -> Path:
    return _run_dir(run_id) / "auto_eval_live_debug.jsonl"


def _meta_from_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": state.get("run_id") or "",
        "stage": "a",
        "dataset": state.get("dataset_path") or "",
        "api_endpoint": state.get("api_endpoint") or "",
        "started_at": state.get("started_at") or "",
        "finished_at": state.get("finished_at") or "",
        "seed_count": int(state.get("seed_count") or 0),
        "seed_list": list(state.get("seed_list") or []),
        "seed_mode": str(state.get("seed_mode") or "random_only"),
        "random_fill_count": int(state.get("random_fill_count") or 0),
        "truncated_count": int(state.get("truncated_count") or 0),
        "script_ids": list(state.get("script_ids") or []),
        "script_titles": list(state.get("script_titles") or []),
        "takes_per_seed": int(state.get("takes_per_seed") or 1),
        "concurrency": int(state.get("concurrency") or settings.seedlab_sample_concurrency),
        "timeout_seconds": int(state.get("timeout_seconds") or DEFAULT_TIMEOUT),
        "retries": int(state.get("retries") or 2),
        "ready_count": int(state.get("ready_count") or 0),
        "failed_count": int(state.get("failed_count") or 0),
        "eval_failed_count": int(state.get("eval_failed_count") or 0),
        "network_fail_count": int(state.get("network_fail_count") or 0),
        "http_502_count": int(state.get("http_502_count") or 0),
        "runpod_job_count": int(state.get("runpod_job_count") or 0),
        "gpu_active_sample_count": int(state.get("gpu_active_sample_count") or 0),
        "remote_eval_failed_count": int(state.get("remote_eval_failed_count") or 0),
        "remote_eval_last_error": str(state.get("remote_eval_last_error") or ""),
        "eval_preflight_status": str(state.get("eval_preflight_status") or ""),
        "eval_preflight_detail": str(state.get("eval_preflight_detail") or ""),
        "eval_preflight_checked_at": str(state.get("eval_preflight_checked_at") or ""),
        "eval_executor_counts": dict(state.get("eval_executor_counts") or {}),
        "avg_stage_timings_ms": dict(state.get("avg_stage_timings_ms") or {}),
    }


def _load_state(run_id: str) -> dict[str, Any]:
    path = _state_path(run_id)
    if not path.exists():
        raise RuntimeError(f"seedlab run not found: {run_id}")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"invalid run state: {run_id}")
    return parsed


def _save_state(state: dict[str, Any]) -> None:
    run_id = str(state.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("run_id is required")
    path = _state_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _utcnow().isoformat()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_gateway_progress_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(state.get("run_id") or ""),
        "status": str(state.get("status") or ""),
        "stage": str(state.get("stage") or ""),
        "eval_location": _eval_location_label(),
        "generated_count": int(state.get("generated_count") or 0),
        "evaluated_count": int(state.get("evaluated_count") or 0),
        "ready_count": int(state.get("ready_count") or 0),
        "failed_count": int(state.get("failed_count") or 0),
        "eval_failed_count": int(state.get("eval_failed_count") or 0),
        "total_count": int(state.get("total_count") or 0),
        "runpod_job_count": int(state.get("runpod_job_count") or 0),
        "gpu_active_sample_count": int(state.get("gpu_active_sample_count") or 0),
        "remote_eval_failed_count": int(state.get("remote_eval_failed_count") or 0),
        "remote_eval_last_error": str(state.get("remote_eval_last_error") or ""),
        "eval_executor_counts": dict(state.get("eval_executor_counts") or {}),
        "avg_stage_timings_ms": dict(state.get("avg_stage_timings_ms") or {}),
        "eval_preflight_status": str(state.get("eval_preflight_status") or ""),
        "eval_preflight_detail": str(state.get("eval_preflight_detail") or ""),
        "eval_preflight_checked_at": str(state.get("eval_preflight_checked_at") or ""),
        "last_error": str(state.get("last_error") or ""),
        "finished_at": str(state.get("finished_at") or ""),
    }


def _post_gateway_progress_payload(payload: dict[str, Any]) -> None:
    gateway_url = (settings.seedlab_gateway_url or "").strip().rstrip("/")
    secret = (settings.gateway_internal_secret or "").strip()
    if not gateway_url or not secret:
        return
    req = urllib.request.Request(
        f"{gateway_url}/internal/seedlab-progress",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Secret": secret},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


async def _notify_gateway_progress(state: dict[str, Any]) -> None:
    payload = _build_gateway_progress_payload(state)
    try:
        await asyncio.to_thread(_post_gateway_progress_payload, payload)
    except Exception as e:
        logger.warning("seedlab progress notify failed run_id=%s err=%s", payload["run_id"], e)


def _notify_gateway_progress_sync(state: dict[str, Any]) -> None:
    payload = _build_gateway_progress_payload(state)
    try:
        _post_gateway_progress_payload(payload)
    except Exception as e:
        logger.warning("seedlab sync progress notify failed run_id=%s err=%s", payload["run_id"], e)


def _update_state(run_id: str, **kwargs: Any) -> dict[str, Any]:
    state = _load_state(run_id)
    state.update(kwargs)
    _save_state(state)
    return state


def _estimate_seedlab_asr_cost_usd(usage: dict[str, Any]) -> float | None:
    duration_sec = float(usage.get("audio_duration_sec") or 0.0)
    rate = float(settings.seedlab_asr_cost_usd_per_minute or 0.0)
    if duration_sec <= 0 or rate <= 0:
        return None
    return (duration_sec / 60.0) * rate


def _estimate_seedlab_judge_cost_usd(usage: dict[str, Any]) -> float | None:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    input_rate = float(settings.seedlab_judge_input_cost_usd_per_1m or 0.0)
    output_rate = float(settings.seedlab_judge_output_cost_usd_per_1m or 0.0)
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return None
    if input_rate <= 0 and output_rate <= 0:
        return None
    return ((prompt_tokens / 1_000_000.0) * input_rate) + ((completion_tokens / 1_000_000.0) * output_rate)


async def _notify_gateway_cost_event(payload: dict[str, Any]) -> None:
    gateway_url = (settings.seedlab_gateway_url or "").strip().rstrip("/")
    secret = (settings.gateway_internal_secret or "").strip()
    if not gateway_url or not secret:
        return

    def _post() -> None:
        req = urllib.request.Request(
            f"{gateway_url}/internal/cost-events",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Internal-Secret": secret},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

    try:
        await asyncio.to_thread(_post)
    except Exception as e:
        logger.warning("seedlab cost notify failed subject=%s err=%s", payload.get("subject_key"), e)


async def _record_seedlab_cost_events(run_id: str, sample_id: str, debug_obj: dict[str, Any]) -> None:
    cost_tracking = debug_obj.get("cost_tracking") if isinstance(debug_obj.get("cost_tracking"), dict) else {}
    subject_key = f"seedlab:{run_id}"
    subject_label = f"SeedLab run {run_id}"
    asr_usage = cost_tracking.get("seedlab_asr") if isinstance(cost_tracking.get("seedlab_asr"), dict) else {}
    judge_usage = cost_tracking.get("seedlab_judge") if isinstance(cost_tracking.get("seedlab_judge"), dict) else {}
    if asr_usage:
        cost_usd = _estimate_seedlab_asr_cost_usd(asr_usage)
        await _notify_gateway_cost_event(
            {
                "job_id": "",
                "stage": "seedlab",
                "process": "seedlab_asr",
                "provider": "openai",
                "attempt_no": 1,
                "status": "success",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ended_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "usage_json": {**asr_usage, "sample_id": sample_id, "executor": debug_obj.get("executor")},
                "raw_response_json": {"source": "seed-lab-service"},
                "cost_usd": cost_usd,
                "pricing_kind": "estimated" if cost_usd is not None else "missing",
                "pricing_source": "provider_usage_estimate" if cost_usd is not None else "unavailable",
                "api_key_family": "seedlab_asr",
                "subject_type": "operation",
                "subject_key": subject_key,
                "subject_label": subject_label,
                "idempotency_key": f"seedlab:asr:{run_id}:{sample_id}",
            }
        )
    if judge_usage:
        cost_usd = _estimate_seedlab_judge_cost_usd(judge_usage)
        await _notify_gateway_cost_event(
            {
                "job_id": "",
                "stage": "seedlab",
                "process": "seedlab_judge",
                "provider": "openai",
                "attempt_no": 1,
                "status": "success",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ended_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "usage_json": {**judge_usage, "sample_id": sample_id, "executor": debug_obj.get("executor")},
                "raw_response_json": {"source": "seed-lab-service"},
                "cost_usd": cost_usd,
                "pricing_kind": "estimated" if cost_usd is not None else "missing",
                "pricing_source": "provider_usage_estimate" if cost_usd is not None else "unavailable",
                "api_key_family": "seedlab_judge",
                "subject_type": "operation",
                "subject_key": subject_key,
                "subject_label": subject_label,
                "idempotency_key": f"seedlab:judge:{run_id}:{sample_id}",
            }
        )


def _merge_stage_timing_averages(state: dict[str, Any], debug_obj: dict[str, Any]) -> None:
    timings = debug_obj.get("stage_timings_ms") if isinstance(debug_obj.get("stage_timings_ms"), dict) else {}
    if not timings:
        return
    totals = state.get("stage_timing_totals_ms") if isinstance(state.get("stage_timing_totals_ms"), dict) else {}
    sample_count = int(state.get("stage_timing_sample_count") or 0) + 1
    for key, value in timings.items():
        if isinstance(value, (int, float)):
            totals[key] = round(float(totals.get(key) or 0.0) + float(value), 3)
    state["stage_timing_totals_ms"] = totals
    state["stage_timing_sample_count"] = sample_count
    state["avg_stage_timings_ms"] = {
        key: round(float(total) / sample_count, 1)
        for key, total in totals.items()
        if isinstance(total, (int, float))
    }


def _record_eval_progress(state: dict[str, Any], eval_obj: dict[str, Any], debug_obj: dict[str, Any]) -> None:
    executor = str(eval_obj.get("executor") or debug_obj.get("executor") or "").strip() or "unknown"
    executor_counts = state.get("eval_executor_counts") if isinstance(state.get("eval_executor_counts"), dict) else {}
    executor_counts[executor] = int(executor_counts.get(executor) or 0) + 1
    state["eval_executor_counts"] = executor_counts
    if bool(debug_obj.get("gpu_acceleration_active")):
        state["gpu_active_sample_count"] = int(state.get("gpu_active_sample_count") or 0) + 1
    if str(eval_obj.get("auto_eval_status") or "").strip().lower() != "ready":
        state["eval_failed_count"] = int(state.get("eval_failed_count") or 0) + 1
    remote_eval = debug_obj.get("remote_eval") if isinstance(debug_obj.get("remote_eval"), dict) else {}
    if str(remote_eval.get("status") or "").strip().lower() == "failed":
        state["remote_eval_failed_count"] = int(state.get("remote_eval_failed_count") or 0) + 1
        state["remote_eval_last_error"] = str(debug_obj.get("error") or remote_eval.get("error") or "")
    _merge_stage_timing_averages(state, debug_obj)


def _normalize_record_for_service(rec: dict[str, Any]) -> dict[str, Any]:
    out = _normalize_record_for_ui(rec)
    rel = str(out.get("audio_rel_path") or "").strip().replace("\\", "/").lstrip("/")
    if rel:
        out["audio_url"] = urllib.parse.quote(rel, safe="/-_.~")
    return out


def _normalize_eval_mode(raw: str) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in {"local", "runpod_pod"} else "runpod_pod"


def _eval_location_label() -> str:
    return "runpod" if _normalize_eval_mode(settings.seedlab_eval_mode) == "runpod_pod" else "local"


def _s3_prefix() -> str:
    return str(settings.seedlab_sample_s3_prefix or "seed-lab-samples").strip().strip("/")


def _sample_s3_bucket() -> str:
    explicit_bucket = str(settings.seedlab_sample_s3_bucket or "").strip()
    if explicit_bucket:
        return explicit_bucket
    return str(settings.media_s3_bucket or "").strip()


def _sample_s3_bucket_source() -> str:
    if str(settings.seedlab_sample_s3_bucket or "").strip():
        return "seedlab"
    if str(settings.media_s3_bucket or "").strip():
        return "media"
    return "none"


def _sample_s3_enabled() -> bool:
    return bool(_sample_s3_bucket())


def _runpod_sample_s3_config_error_detail() -> str:
    return "RunPod eval requires sample S3 upload; missing SEEDLAB_SAMPLE_S3_BUCKET or MEDIA_S3_BUCKET"


def _ensure_runpod_sample_s3_configured() -> None:
    if _normalize_eval_mode(settings.seedlab_eval_mode) == "runpod_pod" and not _sample_s3_enabled():
        raise RunpodEvalError("preflight_unhealthy", _runpod_sample_s3_config_error_detail())


def _runpod_eval_enabled() -> bool:
    return bool(
        _normalize_eval_mode(settings.seedlab_eval_mode) == "runpod_pod"
        and str(settings.seedlab_eval_runpod_url or "").strip()
        and str(settings.seedlab_eval_runpod_shared_secret or "").strip()
    )


def _runpod_eval_missing_config_fields() -> list[str]:
    missing: list[str] = []
    if _normalize_eval_mode(settings.seedlab_eval_mode) != "runpod_pod":
        return missing
    if not str(settings.seedlab_eval_runpod_url or "").strip():
        missing.append("SEEDLAB_EVAL_RUNPOD_URL")
    if not str(settings.seedlab_eval_runpod_shared_secret or "").strip():
        missing.append("SEEDLAB_EVAL_RUNPOD_SHARED_SECRET")
    return missing


def _runpod_eval_config_error_detail() -> str:
    missing = _runpod_eval_missing_config_fields()
    if not missing:
        return "RunPod evaluation config missing"
    return f"RunPod evaluation config missing: {', '.join(missing)}"


def _runpod_eval_base_url() -> str:
    return str(settings.seedlab_eval_runpod_url or "").strip().rstrip("/")


def _runpod_eval_preflight_timeout_seconds() -> float:
    return max(1.0, float(settings.seedlab_eval_runpod_preflight_timeout or 5.0))


def _runpod_eval_request_timeout_seconds(overall_timeout_seconds: float) -> float:
    configured = max(5.0, float(settings.seedlab_eval_runpod_http_timeout or 20.0))
    return min(max(5.0, float(overall_timeout_seconds)), configured)


def _runpod_health_summary_text(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
    chunks = [
        f"ok={snapshot.get('ok')}",
        f"warmup={snapshot.get('warmup_status') or ''}",
        f"job_api={snapshot.get('job_api_enabled')}",
        f"gpu_ready={snapshot.get('gpu_ready')}",
    ]
    if capabilities:
        chunks.append(f"resolved_device={capabilities.get('resolved_device') or ''}")
        chunks.append(f"gpu_active={capabilities.get('gpu_acceleration_active')}")
    warmup_error = str(snapshot.get("warmup_error") or "").strip()
    if warmup_error:
        chunks.append(f"warmup_error={warmup_error[:120]}")
    return ", ".join(chunk for chunk in chunks if chunk and not chunk.endswith("="))


def _runpod_eval_preflight_snapshot() -> dict[str, Any]:
    if not _runpod_eval_enabled():
        raise RunpodEvalError("preflight_unhealthy", _runpod_eval_config_error_detail())
    url = _runpod_eval_base_url()
    timeout_seconds = _runpod_eval_preflight_timeout_seconds()
    req = urllib.request.Request(
        f"{url}/health",
        headers={"X-Seedlab-Secret": str(settings.seedlab_eval_runpod_shared_secret or "").strip()},
        method="GET",
    )
    try:
        payload = _request_json(req, timeout_seconds)
    except Exception as e:
        raise RunpodEvalError("preflight_unhealthy", str(e), endpoint=f"{url}/health") from e
    ok = bool(payload.get("ok"))
    warmup_status = str(payload.get("warmup_status") or "").strip().lower()
    job_api_enabled = bool(payload.get("job_api_enabled"))
    if not ok or warmup_status != "ready" or not job_api_enabled:
        raise RunpodEvalError(
            "preflight_unhealthy",
            "RunPod eval health check failed",
            endpoint=f"{url}/health",
            health_snapshot=payload,
        )
    return payload


def _record_eval_preflight_state(
    state: dict[str, Any],
    *,
    status_value: str,
    detail: str,
    checked_at: str | None = None,
) -> dict[str, Any]:
    state["eval_preflight_status"] = status_value
    state["eval_preflight_detail"] = detail
    state["eval_preflight_checked_at"] = checked_at or _utcnow().isoformat()
    return state


def _record_remote_submit_progress(run_id: str, *, job_id: str, preflight_snapshot: dict[str, Any]) -> None:
    with _state_progress_lock:
        state = _load_state(run_id)
        state["runpod_job_count"] = int(state.get("runpod_job_count") or 0) + 1
        detail = (
            f"remote jobs submitted={int(state.get('runpod_job_count') or 0)}; "
            f"latest_job_id={job_id}; {_runpod_health_summary_text(preflight_snapshot)}"
        )
        _record_eval_preflight_state(state, status_value="ready", detail=detail)
        _save_state(state)
    _notify_gateway_progress_sync(state)


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        region = str(settings.seedlab_sample_s3_region or "").strip() or None
        _s3_client = boto3.client("s3", region_name=region)
    return _s3_client


def _record_audio_s3_key(run_id: str, rec: dict[str, Any]) -> str:
    audio_rel = str(rec.get("audio_rel_path") or "").strip().replace("\\", "/").lstrip("/")
    if not audio_rel:
        raise RuntimeError("audio_rel_path missing")
    return "/".join([part for part in (_s3_prefix(), run_id.strip(), audio_rel) if part])


def _upload_record_audio_to_s3(run_id: str, run_dir: Path, rec: dict[str, Any]) -> tuple[str, str]:
    if not _sample_s3_enabled():
        raise RuntimeError(_runpod_sample_s3_config_error_detail())
    audio_rel = str(rec.get("audio_rel_path") or "").strip().replace("\\", "/").lstrip("/")
    if not audio_rel:
        raise RuntimeError("audio_rel_path missing")
    audio_path = (run_dir / audio_rel).resolve()
    if not audio_path.exists():
        raise RuntimeError(f"audio file missing: {audio_path}")
    key = _record_audio_s3_key(run_id, rec)
    bucket = _sample_s3_bucket()
    extra_args = {"ContentType": "audio/wav"}
    _get_s3_client().upload_file(str(audio_path), bucket, key, ExtraArgs=extra_args)
    uri = f"s3://{bucket}/{key}"
    rec["audio_s3_key"] = key
    rec["audio_s3_uri"] = uri
    return key, uri


def _ensure_record_audio_uploaded(run_id: str, run_dir: Path, rec: dict[str, Any]) -> tuple[str, str]:
    existing_uri = str(rec.get("audio_s3_uri") or "").strip()
    existing_key = str(rec.get("audio_s3_key") or "").strip()
    if existing_uri and existing_key:
        return existing_key, existing_uri
    return _upload_record_audio_to_s3(run_id, run_dir, rec)


def _populate_records_s3_audio(run_id: str, run_dir: Path, records: list[dict[str, Any]], concurrency: int) -> None:
    if not _sample_s3_enabled():
        return
    upload_targets = [rec for rec in records if rec.get("status") in ("ready", "skipped_existing") and str(rec.get("audio_rel_path") or "").strip()]
    if not upload_targets:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(_ensure_record_audio_uploaded, run_id, run_dir, rec): rec for rec in upload_targets}
        for fut, rec in futures.items():
            try:
                fut.result()
            except Exception as e:
                logger.warning("seedlab audio S3 upload failed run_id=%s sample_id=%s err=%s", run_id, rec.get("sample_id"), e)


def _runpod_eval_records_missing_s3_audio(records: list[dict[str, Any]]) -> tuple[int, int, int]:
    targets = [
        rec
        for rec in records
        if rec.get("status") in ("ready", "skipped_existing") and str(rec.get("audio_rel_path") or "").strip()
    ]
    uploaded_count = sum(1 for rec in targets if str(rec.get("audio_s3_uri") or "").strip())
    missing_count = len(targets) - uploaded_count
    return len(targets), uploaded_count, missing_count


def _assert_runpod_records_have_s3_audio(records: list[dict[str, Any]]) -> None:
    if _normalize_eval_mode(settings.seedlab_eval_mode) != "runpod_pod":
        return
    _ensure_runpod_sample_s3_configured()
    total_count, uploaded_count, missing_count = _runpod_eval_records_missing_s3_audio(records)
    if missing_count:
        raise RuntimeError(f"sample_s3_upload_failed: uploaded {uploaded_count}/{total_count}, missing {missing_count}")


def _rewrite_live_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.unlink(missing_ok=True)
    for rec in records:
        _append_jsonl_object(path, rec)


def _local_eval_kwargs() -> dict[str, Any]:
    asr_api_key, judge_api_key = _resolve_openai_keys(
        explicit_shared_key="",
        explicit_asr_key="",
        explicit_judge_key="",
    )
    asr_model_resolved, _warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
    return {
        "asr_api_key": asr_api_key,
        "judge_api_key": judge_api_key,
        "asr_model": asr_model_resolved,
        "judge_model": settings.seedlab_judge_model,
        "language": settings.seedlab_language,
        "timeout_seconds": int(settings.seedlab_auto_eval_timeout),
        "evaluation_profile": settings.seedlab_evaluation_profile,
        "reference_audio_local_path": settings.seedlab_reference_audio_local_path,
        "reference_audio_s3_uri": settings.seedlab_reference_audio_s3_uri,
        "reference_audio_cache_dir": settings.seedlab_reference_audio_cache_dir,
        "disable_llm_note": bool(settings.seedlab_disable_llm_note),
    }


def _remote_eval_request(rec: dict[str, Any]) -> dict[str, Any]:
    audio_s3_uri = str(rec.get("audio_s3_uri") or "").strip()
    if not audio_s3_uri:
        raise RuntimeError("audio_s3_uri missing")
    asr_model_resolved, _warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
    return {
        "sample_id": str(rec.get("sample_id") or "").strip(),
        "seed": int(rec.get("seed") or 0),
        "script_id": str(rec.get("script_id") or "").strip(),
        "script_text": str(rec.get("script_text") or ""),
        "audio_s3_uri": audio_s3_uri,
        "asr_model": asr_model_resolved,
        "judge_model": settings.seedlab_judge_model,
        "language": settings.seedlab_language,
        "timeout_seconds": int(settings.seedlab_auto_eval_timeout),
        "evaluation_profile": settings.seedlab_evaluation_profile,
        "disable_llm_note": bool(settings.seedlab_disable_llm_note),
    }


def _runpod_eval_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Seedlab-Secret": str(settings.seedlab_eval_runpod_shared_secret or "").strip(),
    }


def _request_json(req: urllib.request.Request, timeout_seconds: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=max(5.0, float(timeout_seconds))) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"runpod eval HTTP {e.code}: {detail[:800]}") from e
    except Exception as e:
        raise RuntimeError(f"runpod eval request failed: {e}") from e
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("runpod eval returned non-object response")
    return parsed


def _call_runpod_eval(run_id: str, rec: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if not _runpod_eval_enabled():
        raise RunpodEvalError("preflight_unhealthy", _runpod_eval_config_error_detail())
    url = _runpod_eval_base_url()
    timeout_seconds = max(10, int(settings.seedlab_eval_runpod_timeout))
    poll_interval = max(0.5, float(settings.seedlab_eval_runpod_poll_interval))
    request_timeout = _runpod_eval_request_timeout_seconds(timeout_seconds)
    preflight_snapshot = _runpod_eval_preflight_snapshot()
    payload = _remote_eval_request(rec)
    submit_req = urllib.request.Request(
        f"{url}/evaluate/jobs",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_runpod_eval_headers(),
        method="POST",
    )
    try:
        submitted = _request_json(submit_req, request_timeout)
    except Exception as e:
        raise RunpodEvalError(
            "submit_failed",
            str(e),
            endpoint=f"{url}/evaluate/jobs",
            health_snapshot=preflight_snapshot,
        ) from e
    job_id = str(submitted.get("job_id") or "").strip()
    if not job_id:
        raise RunpodEvalError(
            "invalid_remote_payload",
            "runpod eval submit did not return job_id",
            endpoint=f"{url}/evaluate/jobs",
            health_snapshot=preflight_snapshot,
        )
    submitted_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _record_remote_submit_progress(run_id, job_id=job_id, preflight_snapshot=preflight_snapshot)
    deadline = dt.datetime.now(dt.timezone.utc).timestamp() + timeout_seconds
    parsed = submitted
    while True:
        status_value = str(parsed.get("status") or "").strip().lower()
        if status_value in {"completed", "succeeded"}:
            break
        if status_value in {"failed", "error"}:
            detail = str(parsed.get("error") or "unknown error")
            raise RunpodEvalError(
                "remote_job_failed",
                detail,
                endpoint=f"{url}/evaluate/jobs/{job_id}",
                remote_job_id=job_id,
                health_snapshot=preflight_snapshot,
            )
        if dt.datetime.now(dt.timezone.utc).timestamp() >= deadline:
            raise RunpodEvalError(
                "poll_timeout",
                f"runpod eval polling timeout job_id={job_id}",
                endpoint=f"{url}/evaluate/jobs/{job_id}",
                remote_job_id=job_id,
                health_snapshot=preflight_snapshot,
            )
        time.sleep(poll_interval)
        poll_req = urllib.request.Request(
            f"{url}/evaluate/jobs/{urllib.parse.quote(job_id, safe='')}",
            headers=_runpod_eval_headers(),
            method="GET",
        )
        try:
            parsed = _request_json(poll_req, request_timeout)
        except Exception as e:
            raise RunpodEvalError(
                "poll_timeout",
                str(e),
                endpoint=f"{url}/evaluate/jobs/{job_id}",
                remote_job_id=job_id,
                health_snapshot=preflight_snapshot,
            ) from e
    sample_id = str(parsed.get("sample_id") or payload["sample_id"]).strip()
    evaluation = parsed.get("evaluation")
    debug = parsed.get("debug")
    if not sample_id or not isinstance(evaluation, dict) or not isinstance(debug, dict):
        raise RunpodEvalError(
            "invalid_remote_payload",
            "runpod eval returned invalid payload",
            endpoint=f"{url}/evaluate/jobs/{job_id}",
            remote_job_id=job_id,
            health_snapshot=preflight_snapshot,
        )
    evaluation["executor"] = str(evaluation.get("executor") or "runpod_gpu")
    debug["executor"] = str(debug.get("executor") or "runpod_gpu")
    remote_eval = debug.get("remote_eval") if isinstance(debug.get("remote_eval"), dict) else {}
    remote_eval.update(
        {
            "executor": "runpod_gpu",
            "job_id": job_id,
            "submitted_at": submitted_at,
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "completed",
            "gpu_acceleration_active": bool(debug.get("gpu_acceleration_active")),
            "health_summary": _runpod_health_summary_text(preflight_snapshot),
        }
    )
    debug["remote_eval"] = remote_eval
    debug["runpod_health"] = preflight_snapshot
    return sample_id, evaluation, debug


def _evaluate_record_with_fallback(run_id: str, run_dir: Path, rec: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if _normalize_eval_mode(settings.seedlab_eval_mode) == "runpod_pod":
        _ensure_runpod_sample_s3_configured()
        _ensure_record_audio_uploaded(run_id, run_dir, rec)
        return _call_runpod_eval(run_id, rec)
    return _auto_eval_single_record(run_dir=run_dir, rec=rec, **_local_eval_kwargs())


def _run_status_payload(run_id: str) -> dict[str, Any]:
    state = _load_state(run_id)
    return {
        "run_id": run_id,
        "status": str(state.get("status") or "queued"),
        "stage": str(state.get("stage") or ""),
        "generated_count": int(state.get("generated_count") or 0),
        "failed_count": int(state.get("failed_count") or 0),
        "evaluated_count": int(state.get("evaluated_count") or 0),
        "total_count": int(state.get("total_count") or 0),
        "eval_failed_count": int(state.get("eval_failed_count") or 0),
        "runpod_job_count": int(state.get("runpod_job_count") or 0),
        "gpu_active_sample_count": int(state.get("gpu_active_sample_count") or 0),
        "remote_eval_failed_count": int(state.get("remote_eval_failed_count") or 0),
        "remote_eval_last_error": str(state.get("remote_eval_last_error") or ""),
        "eval_executor_counts": dict(state.get("eval_executor_counts") or {}),
        "avg_stage_timings_ms": dict(state.get("avg_stage_timings_ms") or {}),
        "eval_preflight_status": str(state.get("eval_preflight_status") or ""),
        "eval_preflight_detail": str(state.get("eval_preflight_detail") or ""),
        "eval_preflight_checked_at": str(state.get("eval_preflight_checked_at") or ""),
        "last_error": str(state.get("last_error") or ""),
        "started_at": str(state.get("started_at") or ""),
        "finished_at": str(state.get("finished_at") or ""),
    }


def _write_initial_run_files(state: dict[str, Any]) -> None:
    run_id = str(state["run_id"])
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(run_dir, _meta_from_state(state), [])
    _generate_review_html(run_id, [], run_dir / "index.html")
    _write_human_eval_map(run_dir / HUMAN_EVAL_JSON, run_id=run_id, evaluations={})


_queue: asyncio.Queue[str] = asyncio.Queue()
_worker_tasks: list[asyncio.Task[Any]] = []
_queue_lock = asyncio.Lock()


def _openai_enabled() -> bool:
    if _runpod_eval_enabled():
        return True
    try:
        _resolve_openai_keys(explicit_shared_key="", explicit_asr_key="", explicit_judge_key="")
        return True
    except Exception:
        return False


def _seedlab_capabilities() -> dict[str, Any]:
    return {
        **_seedlab_runtime_capabilities(
            reference_audio_local_path=settings.seedlab_reference_audio_local_path,
            reference_audio_s3_uri=settings.seedlab_reference_audio_s3_uri,
        ),
        "eval_mode": _normalize_eval_mode(settings.seedlab_eval_mode),
        "runpod_eval_enabled": _runpod_eval_enabled(),
        "runpod_eval_missing_config_fields": _runpod_eval_missing_config_fields(),
        "sample_s3_enabled": _sample_s3_enabled(),
        "sample_s3_bucket_configured": bool(_sample_s3_bucket()),
        "sample_s3_bucket_source": _sample_s3_bucket_source(),
        "reference_audio_local_path_configured": bool((settings.seedlab_reference_audio_local_path or "").strip()),
        "reference_audio_s3_uri_configured": bool((settings.seedlab_reference_audio_s3_uri or "").strip()),
    }


async def _run_generation_pipeline(run_id: str) -> None:
    state = _load_state(run_id)
    run_dir = _run_dir(run_id)
    dataset_path = Path(str(state["dataset_path"])).resolve()
    scripts, tts_params = load_dataset(dataset_path)
    endpoint = _resolve_api_endpoint(state.get("api_endpoint") or settings.tts_router_url or settings.tts_api_url)
    selected_scripts = _pick_scripts_for_stage(scripts, stage="a", script_ids=[])
    seed_list = list(state.get("seed_list") or [])
    takes_per_seed = int(state.get("takes_per_seed") or 1)
    concurrency = int(state.get("concurrency") or settings.seedlab_sample_concurrency)
    retries = int(state.get("retries") or 2)
    timeout_seconds = int(state.get("timeout_seconds") or DEFAULT_TIMEOUT)

    tasks: list[tuple[int, Any, int]] = []
    for seed in seed_list:
        for script in selected_scripts:
            for take_index in range(1, takes_per_seed + 1):
                tasks.append((int(seed), script, take_index))

    total_count = len(tasks)
    state = _update_state(
        run_id,
        status="generating",
        stage="generating",
        started_at=state.get("started_at") or _utcnow().isoformat(),
        total_count=total_count,
        generated_count=0,
        failed_count=0,
        ready_count=0,
    )

    records: list[dict[str, Any]] = []
    live_records_path = run_dir / LIVE_RECORDS_JSONL
    live_records_path.unlink(missing_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [
            pool.submit(
                _worker_generate_one,
                endpoint,
                timeout_seconds,
                tts_params,
                run_dir,
                seed,
                script,
                take_index,
                retries,
            )
            for seed, script, take_index in tasks
        ]
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            records.append(rec)
            done_count += 1
            if rec.get("status") in ("ready", "skipped_existing"):
                state["ready_count"] = int(state.get("ready_count") or 0) + 1
            else:
                state["failed_count"] = int(state.get("failed_count") or 0) + 1
            state["generated_count"] = done_count
            _save_state(state)
            await _notify_gateway_progress(state)
            try:
                _append_jsonl_object(live_records_path, rec)
            except Exception:
                pass

    records.sort(key=lambda r: (str(r.get("script_id", "")), int(r.get("seed", 0)), int(r.get("take_index", 0))))
    network_fail_count = sum(
        1 for r in records if r.get("status") == "failed" and r.get("error_type") == "network_origin_unreachable"
    )
    http_502_count = sum(
        1 for r in records if r.get("status") == "failed" and "http 502" in str(r.get("error", "")).lower()
    )
    state["network_fail_count"] = network_fail_count
    state["http_502_count"] = http_502_count
    try:
        _populate_records_s3_audio(run_id, run_dir, records, concurrency)
        _assert_runpod_records_have_s3_audio(records)
    except Exception as e:
        _write_manifest(run_dir, _meta_from_state(state), records)
        _rewrite_live_records(live_records_path, records)
        state["remote_eval_failed_count"] = int(state.get("remote_eval_failed_count") or 0) + 1
        state["remote_eval_last_error"] = str(e)
        state["last_error"] = str(e)
        state["status"] = "failed"
        state["stage"] = "failed"
        state["finished_at"] = _utcnow().isoformat()
        _save_state(state)
        await _notify_gateway_progress(state)
        raise
    _write_manifest(run_dir, _meta_from_state(state), records)
    _rewrite_live_records(live_records_path, records)
    _generate_review_html(run_id, records, run_dir / "index.html")

    if _openai_enabled() and records:
        state = _update_state(
            run_id,
            status="auto_evaluating",
            stage="auto_evaluating",
            evaluated_count=0,
            eval_failed_count=0,
            runpod_job_count=0,
            gpu_active_sample_count=0,
            remote_eval_failed_count=0,
            remote_eval_last_error="",
            eval_preflight_status="pending",
            eval_preflight_detail="",
            eval_preflight_checked_at="",
            eval_executor_counts={},
            stage_timing_totals_ms={},
            stage_timing_sample_count=0,
            avg_stage_timings_ms={},
        )
        await _notify_gateway_progress(state)
        if _normalize_eval_mode(settings.seedlab_eval_mode) == "runpod_pod":
            try:
                preflight_snapshot = await asyncio.to_thread(_runpod_eval_preflight_snapshot)
                state = _load_state(run_id)
                _record_eval_preflight_state(
                    state,
                    status_value="ready",
                    detail=f"remote eval ready; {_runpod_health_summary_text(preflight_snapshot)}",
                )
                _save_state(state)
                await _notify_gateway_progress(state)
            except RunpodEvalError as e:
                state = _load_state(run_id)
                _record_eval_preflight_state(state, status_value="failed", detail=str(e))
                state["remote_eval_failed_count"] = int(state.get("remote_eval_failed_count") or 0) + 1
                state["remote_eval_last_error"] = str(e)
                state["last_error"] = str(e)
                _save_state(state)
                await _notify_gateway_progress(state)
                raise
        asr_model_resolved, _asr_warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
        eval_out = run_dir / "auto_eval.json"
        eval_out.unlink(missing_ok=True)
        _live_eval_debug_path(run_id).unlink(missing_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [
                pool.submit(
                    _evaluate_record_with_fallback,
                    run_id,
                    run_dir,
                    rec,
                )
                for rec in records
                if rec.get("status") in ("ready", "skipped_existing")
            ]
            eval_done = 0
            for fut in concurrent.futures.as_completed(futures):
                try:
                    sample_id, eval_obj, debug_obj = fut.result()
                except Exception as e:
                    state = _load_state(run_id)
                    state["remote_eval_failed_count"] = int(state.get("remote_eval_failed_count") or 0) + 1
                    state["remote_eval_last_error"] = str(e)
                    state["last_error"] = str(e)
                    _save_state(state)
                    await _notify_gateway_progress(state)
                    raise
                _upsert_eval_entry(
                    eval_out,
                    run_id=run_id,
                    sample_id=sample_id,
                    eval_obj=eval_obj,
                    asr_model=asr_model_resolved,
                    judge_model=settings.seedlab_judge_model,
                )
                debug_path = _live_eval_debug_path(run_id)
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                with debug_path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(debug_obj, ensure_ascii=False) + "\n")
                eval_done += 1
                state = _load_state(run_id)
                state["evaluated_count"] = eval_done
                _record_eval_progress(state, eval_obj, debug_obj)
                await _record_seedlab_cost_events(run_id, sample_id, debug_obj)
                _save_state(state)
                await _notify_gateway_progress(state)

    state = _update_state(
        run_id,
        status="ready",
        stage="ready",
        finished_at=_utcnow().isoformat(),
        generated_count=len(records),
        total_count=len(records),
    )
    _write_manifest(run_dir, _meta_from_state(state), records)
    _generate_review_html(run_id, records, run_dir / "index.html")
    await _notify_gateway_progress(state)


async def _queue_worker() -> None:
    while True:
        run_id = await _queue.get()
        try:
            await _run_generation_pipeline(run_id)
        except Exception as e:
            logger.exception("seedlab run failed run_id=%s", run_id)
            try:
                failed_state = _update_state(
                    run_id,
                    status="failed",
                    stage="failed",
                    finished_at=_utcnow().isoformat(),
                    last_error=str(e),
                )
                await _notify_gateway_progress(failed_state)
            except Exception:
                logger.exception("seedlab failed state update run_id=%s", run_id)
        finally:
            _queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_root, run_root_writable, run_root_error = _ensure_run_root_writable()
    if not run_root_writable:
        raise RuntimeError(f"seedlab run root is not writable: {run_root} ({run_root_error})")
    missing_runpod_config = _runpod_eval_missing_config_fields()
    if missing_runpod_config:
        logger.warning(
            "seed-lab-service runpod eval config incomplete eval_mode=%s missing=%s",
            _normalize_eval_mode(settings.seedlab_eval_mode),
            ",".join(missing_runpod_config),
        )
    for _ in range(max(1, int(settings.seedlab_queue_concurrency))):
        _worker_tasks.append(asyncio.create_task(_queue_worker()))
    logger.info("seed-lab-service started")
    yield
    for task in _worker_tasks:
        task.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)


app = FastAPI(title="Seed Lab Service", lifespan=lifespan)


async def verify_secret(x_internal_secret: Optional[str] = Header(default=None)) -> None:
    if x_internal_secret != settings.gateway_internal_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-Internal-Secret header")


AuthDep = Depends(verify_secret)


def _resolve_run_file(run_id: str, rel_path: str) -> Path:
    run_dir = _run_dir(run_id)
    target = (run_dir / rel_path).resolve()
    if not str(target).startswith(str(run_dir)):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return target


@app.post("/internal/runs")
async def create_run(_: Any = AuthDep, body: RunCreateRequest | None = None) -> dict[str, Any]:
    run_root, run_root_writable, run_root_error = _ensure_run_root_writable()
    if not run_root_writable:
        raise HTTPException(status_code=500, detail=f"seedlab run root is not writable: {run_root} ({run_root_error})")
    payload = body or RunCreateRequest()
    dataset_path = Path(payload.dataset_path or settings.seedlab_default_dataset).resolve()
    scripts, _tts_params = load_dataset(dataset_path)
    selected_scripts = _pick_scripts_for_stage(scripts, stage="a", script_ids=[])
    requested_samples = 10 if payload.dup else 30
    takes_per_seed = 3 if payload.dup else 1
    if payload.seeds.strip():
        seed_list, random_fill_count, truncated_count = _expand_seed_list_with_random(
            _parse_seed_values(payload.seeds),
            requested_samples,
        )
        seed_mode = "explicit_plus_random_fill"
    else:
        from seed_lab import _random_unique_seeds  # type: ignore

        seed_list = _random_unique_seeds(requested_samples)
        random_fill_count = 0
        truncated_count = 0
        seed_mode = "random_only"
    run_id = payload.run_id.strip() or _build_run_id("a")
    state = {
        "run_id": run_id,
        "status": "queued",
        "stage": "queued",
        "dataset_path": str(dataset_path),
        "api_endpoint": _resolve_api_endpoint(settings.tts_router_url or settings.tts_api_url),
        "seed_list": seed_list,
        "seed_count": len(seed_list),
        "seed_mode": seed_mode,
        "random_fill_count": random_fill_count,
        "truncated_count": truncated_count,
        "script_ids": [s.script_id for s in selected_scripts],
        "script_titles": [s.title for s in selected_scripts],
        "takes_per_seed": takes_per_seed,
        "concurrency": int(settings.seedlab_sample_concurrency),
        "timeout_seconds": DEFAULT_TIMEOUT,
        "retries": 2,
        "dup_mode": bool(payload.dup),
        "generated_count": 0,
        "evaluated_count": 0,
        "failed_count": 0,
        "eval_failed_count": 0,
        "ready_count": 0,
        "runpod_job_count": 0,
        "gpu_active_sample_count": 0,
        "remote_eval_failed_count": 0,
        "remote_eval_last_error": "",
        "eval_preflight_status": "pending" if _normalize_eval_mode(settings.seedlab_eval_mode) == "runpod_pod" else "",
        "eval_preflight_detail": "",
        "eval_preflight_checked_at": "",
        "eval_executor_counts": {},
        "stage_timing_totals_ms": {},
        "stage_timing_sample_count": 0,
        "avg_stage_timings_ms": {},
        "total_count": requested_samples * takes_per_seed * len(selected_scripts),
        "last_error": "",
        "created_at": _utcnow().isoformat(),
        "started_at": "",
        "finished_at": "",
    }
    if _normalize_eval_mode(settings.seedlab_eval_mode) == "runpod_pod":
        try:
            _ensure_runpod_sample_s3_configured()
            preflight_snapshot = await asyncio.to_thread(_runpod_eval_preflight_snapshot)
            _record_eval_preflight_state(
                state,
                status_value="ready",
                detail=f"remote eval ready; {_runpod_health_summary_text(preflight_snapshot)}",
            )
        except RunpodEvalError as e:
            _record_eval_preflight_state(state, status_value="failed", detail=str(e))
            raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        _write_initial_run_files(state)
        _save_state(state)
    except Exception as e:
        logger.exception("seedlab create_run failed run_id=%s", run_id)
        raise HTTPException(status_code=500, detail=f"seedlab create failed: {e}") from e
    await _queue.put(run_id)
    await _notify_gateway_progress(state)
    return {"ok": True, "run_id": run_id, "status": "queued"}


@app.get("/internal/runs/{run_id}/status")
async def internal_run_status(run_id: str, _: Any = AuthDep) -> dict[str, Any]:
    return _run_status_payload(run_id)


@app.get("/runs/{run_id}/")
async def run_index(run_id: str) -> Response:
    index = _resolve_run_file(run_id, "index.html")
    return FileResponse(index, media_type="text/html; charset=utf-8")


@app.get("/runs/{run_id}/api/config")
async def run_config(run_id: str) -> dict[str, Any]:
    state = _load_state(run_id)
    run_dir = _run_dir(run_id)
    default_tts_params = _resolve_serve_default_tts_params(_meta_from_state(state), run_dir)
    asr_model_resolved, asr_warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
    capabilities = _seedlab_capabilities()
    return {
        "run_id": run_id,
        "tts_endpoint": _resolve_api_endpoint(settings.tts_router_url or settings.tts_api_url),
        "default_tts_params": default_tts_params,
        "auto_eval_on_add": _openai_enabled(),
        "openai_configured": _openai_enabled(),
        "asr_model": asr_model_resolved,
        "asr_model_requested": settings.seedlab_asr_model,
        "judge_model": settings.seedlab_judge_model,
        "evaluation_profile": settings.seedlab_evaluation_profile,
        "asr_warning": asr_warning,
        "eval_mode": _normalize_eval_mode(settings.seedlab_eval_mode),
        "runpod_eval_enabled": _runpod_eval_enabled(),
        "sample_s3_enabled": _sample_s3_enabled(),
        "capabilities": capabilities,
    }


@app.get("/runs/{run_id}/api/run-status")
async def run_status(run_id: str) -> dict[str, Any]:
    return _run_status_payload(run_id)


@app.get("/runs/{run_id}/api/live-records")
async def run_live_records(run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    manifest_records: list[dict[str, Any]] = []
    try:
        _meta, manifest_records = _load_manifest(run_dir)
    except Exception:
        manifest_records = []
    rows = [_normalize_record_for_service(r) for r in _read_jsonl_objects(run_dir / LIVE_RECORDS_JSONL)]
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    merged = {_normalize_record_for_service(r).get("sample_id"): _normalize_record_for_service(r) for r in manifest_records}
    for row in rows:
        merged[str(row.get("sample_id") or "")] = row
    return {"run_id": run_id, "records": [r for r in merged.values() if isinstance(r, dict)]}


@app.get("/runs/{run_id}/api/ai-evals")
async def run_ai_evals(run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    merged = _merge_eval_maps([run_dir / "auto_eval.json", run_dir / LIVE_AUTO_EVAL_JSON])
    return {"run_id": run_id, "evaluations": merged}


@app.get("/runs/{run_id}/api/human-evals")
async def run_human_evals(run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    return {"run_id": run_id, "evaluations": _load_human_eval_map(run_dir / HUMAN_EVAL_JSON)}


@app.put("/runs/{run_id}/api/human-evals")
async def update_human_evals(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    evaluations = body.get("evaluations")
    if not isinstance(evaluations, dict):
        raise HTTPException(status_code=400, detail="evaluations must be an object")
    normalized: dict[str, Any] = {}
    for sample_id, eval_obj in evaluations.items():
        if isinstance(eval_obj, dict):
            normalized[str(sample_id)] = eval_obj
    _write_human_eval_map(_run_dir(run_id) / HUMAN_EVAL_JSON, run_id=run_id, evaluations=normalized)
    return {"ok": True, "run_id": run_id, "saved_count": len(normalized)}


@app.post("/runs/{run_id}/api/tts/generate")
async def run_tts_generate(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(run_id)
    meta, manifest_records = _load_manifest(run_dir)
    default_tts_params = _resolve_serve_default_tts_params(meta, run_dir)
    script_text = str(body.get("script_text") or "").strip()
    if not script_text:
        raise HTTPException(status_code=400, detail="script_text is required")
    seed_raw = str(body.get("seed") or "").strip()
    seed = _to_int(seed_raw, default=1, min_value=1, max_value=2_147_483_647) if seed_raw else __import__("random").randint(1, 2_147_483_647)
    tts_params = dict(default_tts_params)
    if "text_lang" in body:
        tts_params["text_lang"] = str(body.get("text_lang") or "").strip() or "ko"
    if "prompt_lang" in body:
        tts_params["prompt_lang"] = str(body.get("prompt_lang") or "").strip() or tts_params.get("text_lang", "ko")
    if "top_k" in body:
        tts_params["top_k"] = _to_int(body.get("top_k"), default=20, min_value=1, max_value=100)
    if "sample_steps" in body:
        tts_params["sample_steps"] = _to_int(body.get("sample_steps"), default=32, min_value=1, max_value=128)
    if "fragment_interval" in body:
        tts_params["fragment_interval"] = _to_float(body.get("fragment_interval"), default=0.4, min_value=0.0, max_value=5.0)
    if "super_sampling" in body:
        tts_params["super_sampling"] = _to_bool(body.get("super_sampling"), default=True)
    if "ref_audio_path" in body:
        ref_audio_path = str(body.get("ref_audio_path") or "").strip()
        if ref_audio_path:
            tts_params["ref_audio_path"] = ref_audio_path
    if "prompt_text" in body:
        prompt_text = str(body.get("prompt_text") or "").strip()
        if prompt_text:
            tts_params["prompt_text"] = prompt_text
    endpoint = _resolve_api_endpoint(settings.tts_router_url or settings.tts_api_url)
    rec = await asyncio.to_thread(
        _worker_generate_one,
        endpoint,
        DEFAULT_TIMEOUT,
        tts_params,
        run_dir,
        seed,
        type("InlineScript", (), {"script_id": str(body.get("script_id") or "live"), "title": str(body.get("script_title") or "즉석 생성"), "text": script_text}),
        1,
        2,
    )
    if rec.get("status") != "ready":
        raise HTTPException(status_code=500, detail=str(rec.get("error") or "tts generate failed"))
    created = _utcnow()
    rec["created_at"] = created.isoformat()
    rec["audio_url"] = urllib.parse.quote(str(rec.get("audio_rel_path") or "").strip().replace("\\", "/"), safe="/-_.~")
    if _sample_s3_enabled():
        try:
            await asyncio.to_thread(_ensure_record_audio_uploaded, run_id, run_dir, rec)
        except Exception as e:
            logger.warning("seedlab live audio S3 upload failed run_id=%s sample_id=%s err=%s", run_id, rec.get("sample_id"), e)
    if _to_bool(body.get("add_to_review"), default=False):
        _append_jsonl_object(run_dir / LIVE_RECORDS_JSONL, rec)
    ai_eval_obj = None
    if _to_bool(body.get("add_to_review"), default=False) and _openai_enabled():
        asr_model_resolved, _warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
        sample_id_eval, eval_obj, debug_obj = await asyncio.to_thread(_evaluate_record_with_fallback, run_id, run_dir, rec)
        _upsert_eval_entry(
            run_dir / LIVE_AUTO_EVAL_JSON,
            run_id=run_id,
            sample_id=sample_id_eval,
            eval_obj=eval_obj,
            asr_model=asr_model_resolved,
            judge_model=settings.seedlab_judge_model,
        )
        with _live_eval_debug_path(run_id).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(debug_obj, ensure_ascii=False) + "\n")
        ai_eval_obj = eval_obj
    return {"ok": True, "run_id": run_id, "seed": seed, "audio_url": rec["audio_url"], "record": rec, "ai_eval": ai_eval_obj}


@app.post("/runs/{run_id}/api/ai-eval-one")
async def run_ai_eval_one(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if not _openai_enabled():
        raise HTTPException(status_code=400, detail="OPENAI seedlab keys are not configured")
    sample_id = str(body.get("sample_id") or "").strip()
    if not sample_id:
        raise HTTPException(status_code=400, detail="sample_id is required")
    run_dir = _run_dir(run_id)
    rec = None
    try:
        _meta, manifest_records = _load_manifest(run_dir)
    except Exception:
        manifest_records = []
    for row in manifest_records + _read_jsonl_objects(run_dir / LIVE_RECORDS_JSONL):
        if str(row.get("sample_id") or "") == sample_id:
            rec = row
            break
    if not rec:
        raise HTTPException(status_code=404, detail="record not found")
    asr_model_resolved, _warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
    sample_id_eval, eval_obj, debug_obj = await asyncio.to_thread(_evaluate_record_with_fallback, run_id, run_dir, rec)
    target_path = run_dir / LIVE_AUTO_EVAL_JSON if sample_id.startswith("live:") else run_dir / "auto_eval.json"
    _upsert_eval_entry(
        target_path,
        run_id=run_id,
        sample_id=sample_id_eval,
        eval_obj=eval_obj,
        asr_model=asr_model_resolved,
        judge_model=settings.seedlab_judge_model,
    )
    with _live_eval_debug_path(run_id).open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(debug_obj, ensure_ascii=False) + "\n")
    return {"ok": True, "sample_id": sample_id_eval, "evaluation": eval_obj}


@app.get("/runs/{run_id}/{asset_path:path}")
async def run_asset(run_id: str, asset_path: str) -> Response:
    target = _resolve_run_file(run_id, asset_path)
    if target.suffix.lower() == ".html":
        return HTMLResponse(target.read_text(encoding="utf-8"))
    return FileResponse(target)


@app.get("/health")
async def health() -> dict[str, Any]:
    run_root, run_root_writable, run_root_error = _ensure_run_root_writable()
    capabilities = _seedlab_capabilities()
    return {
        "status": "ok" if run_root_writable else "degraded",
        "queue_size": _queue.qsize(),
        "run_root": str(run_root),
        "run_root_writable": run_root_writable,
        "run_root_error": run_root_error,
        "capabilities": capabilities,
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return await health()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.seedlab_port)
