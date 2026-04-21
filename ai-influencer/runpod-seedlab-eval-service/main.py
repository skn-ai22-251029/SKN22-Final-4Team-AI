from __future__ import annotations

import datetime as dt
import logging
import secrets
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

import boto3
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel
from pydantic_settings import BaseSettings

sys.path.insert(0, str((Path(__file__).resolve().parent.parent / "scripts").resolve()))

from seed_lab import (  # type: ignore
    DEFAULT_AUTO_EVAL_ASR_MODEL,
    DEFAULT_AUTO_EVAL_JUDGE_MODEL,
    DEFAULT_AUTO_EVAL_PROFILE,
    _auto_eval_audio_file,
    _infer_module_device,
    _load_distillmos_predictor,
    _load_speaker_verifier,
    _resolve_asr_model_for_transcription,
    _resolve_reference_audio_paths,
    _resolve_seedlab_eval_runtime,
    _resolve_openai_keys,
    _seedlab_runtime_capabilities,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("runpod-seedlab-eval")


class Settings(BaseSettings):
    seedlab_eval_port: int = 8400
    seedlab_eval_shared_secret: str
    openai_fallback_api_key: str = ""
    openai_api_key_seedlab_asr: str = ""
    openai_api_key_seedlab_judge: str = ""
    seedlab_asr_model: str = DEFAULT_AUTO_EVAL_ASR_MODEL
    seedlab_judge_model: str = DEFAULT_AUTO_EVAL_JUDGE_MODEL
    seedlab_auto_eval_timeout: int = 120
    seedlab_evaluation_profile: str = DEFAULT_AUTO_EVAL_PROFILE
    seedlab_reference_audio_local_path: str = ""
    seedlab_reference_audio_s3_uri: str = ""
    seedlab_reference_audio_cache_dir: str = "/tmp/seedlab-ref-cache"
    seedlab_disable_llm_note: bool = False
    seedlab_language: str = "ko"
    seedlab_eval_job_dir: str = "/workspace/runpod-stack/state/seedlab-eval-jobs"
    aws_default_region: str = "ap-northeast-2"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
_GPU_RUNTIME = _resolve_seedlab_eval_runtime()
_WARMUP_STATE: dict[str, Any] = {
    "status": "pending",
    "error": "",
    "completed_at": "",
    "reference_count_loaded": 0,
    "reference_audio_source": "",
    "reference_set_id": "",
    "mos_device": "",
    "speaker_device": "",
    "gpu_ready": False,
}
_WARMUP_THREAD: Optional[threading.Thread] = None
app = FastAPI(title="Runpod Seed Lab Eval Service")


class EvaluateRequest(BaseModel):
    sample_id: str
    seed: int = 0
    script_id: str = ""
    script_text: str
    audio_s3_uri: str
    asr_model: str = DEFAULT_AUTO_EVAL_ASR_MODEL
    judge_model: str = DEFAULT_AUTO_EVAL_JUDGE_MODEL
    language: str = "ko"
    timeout_seconds: int = 120
    evaluation_profile: str = DEFAULT_AUTO_EVAL_PROFILE
    disable_llm_note: bool = False


def _job_dir() -> Path:
    path = Path(settings.seedlab_eval_job_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(job_id: str) -> Path:
    return _job_dir() / f"{job_id}.json"


def _write_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["job_id"] = job_id
    _job_path(job_id).write_text(json_dumps(payload), encoding="utf-8")
    return payload


def _read_job(job_id: str) -> dict[str, Any]:
    path = _job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    parsed = json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="invalid job state")
    return parsed


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def json_loads(raw: str) -> Any:
    import json

    return json.loads(raw)


def _get_s3_client():
    return boto3.client("s3", region_name=(settings.aws_default_region or "").strip() or None)


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    raw = str(uri or "").strip()
    if not raw.startswith("s3://"):
        raise RuntimeError("audio_s3_uri must start with s3://")
    bucket_and_key = raw[5:]
    if "/" not in bucket_and_key:
        raise RuntimeError("audio_s3_uri missing key")
    return bucket_and_key.split("/", 1)


def _download_audio_to_temp(audio_s3_uri: str, tmp_dir: Path) -> Path:
    bucket, key = _parse_s3_uri(audio_s3_uri)
    target = tmp_dir / Path(key).name
    _get_s3_client().download_file(bucket, key, str(target))
    return target


def _warmup_runtime_state() -> dict[str, Any]:
    runtime = _resolve_seedlab_eval_runtime()
    resolved_device = str(runtime.get("resolved_device") or "cpu")
    logger.info("seedlab warmup starting resolved_device=%s", resolved_device)
    reference_audio_paths, reference_audio_source, reference_set_id = _resolve_reference_audio_paths(
        run_dir=_job_dir(),
        rec={},
        explicit_local_path=settings.seedlab_reference_audio_local_path,
        explicit_s3_uri=settings.seedlab_reference_audio_s3_uri,
        reference_audio_cache_dir=settings.seedlab_reference_audio_cache_dir,
    )
    logger.info(
        "seedlab warmup references ready count=%s source=%s set_id=%s",
        len(reference_audio_paths),
        reference_audio_source or "unknown",
        reference_set_id or "unknown",
    )
    mos_device = ""
    speaker_device = ""
    if resolved_device:
        logger.info("seedlab warmup loading distillmos device=%s", resolved_device)
        model = _load_distillmos_predictor(resolved_device)
        mos_device = _infer_module_device(model) or resolved_device
        logger.info("seedlab warmup distillmos ready device=%s", mos_device)
        logger.info("seedlab warmup loading speaker verifier device=%s", resolved_device)
        verifier = _load_speaker_verifier(resolved_device)
        speaker_device = _infer_module_device(verifier) or str(getattr(verifier, "device", "") or resolved_device)
        logger.info("seedlab warmup speaker verifier ready device=%s", speaker_device)
    gpu_ready = (
        str(runtime.get("resolved_device") or "").startswith("cuda")
        and str(mos_device or "").startswith("cuda")
        and str(speaker_device or "").startswith("cuda")
    )
    logger.info(
        "seedlab warmup completed gpu_ready=%s reference_count=%s mos_device=%s speaker_device=%s",
        gpu_ready,
        len(reference_audio_paths),
        mos_device or "unknown",
        speaker_device or "unknown",
    )
    return {
        "status": "ready",
        "error": "",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "reference_count_loaded": len(reference_audio_paths),
        "reference_audio_source": reference_audio_source,
        "reference_set_id": reference_set_id,
        "mos_device": mos_device,
        "speaker_device": speaker_device,
        "gpu_ready": gpu_ready,
    }


def _ensure_runtime_warmup() -> None:
    global _WARMUP_STATE
    _WARMUP_STATE = {
        **_WARMUP_STATE,
        "status": "warming",
        "error": "",
    }
    try:
        _WARMUP_STATE = _warmup_runtime_state()
    except Exception as e:
        _WARMUP_STATE = {
            **_WARMUP_STATE,
            "status": "failed",
            "error": str(e),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "gpu_ready": False,
        }
        logger.exception("seedlab warmup failed")


def _start_runtime_warmup() -> None:
    global _WARMUP_THREAD, _WARMUP_STATE
    if _WARMUP_THREAD is not None and _WARMUP_THREAD.is_alive():
        return
    _WARMUP_STATE = {
        **_WARMUP_STATE,
        "status": "warming",
        "error": "",
        "completed_at": "",
        "gpu_ready": False,
    }
    _WARMUP_THREAD = threading.Thread(target=_ensure_runtime_warmup, name="seedlab-warmup", daemon=True)
    _WARMUP_THREAD.start()


def _assert_runtime_ready() -> None:
    if str(_WARMUP_STATE.get("status") or "") != "ready":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"seedlab runtime warmup is not ready: {str(_WARMUP_STATE.get('error') or _WARMUP_STATE.get('status') or 'unknown')}",
        )


async def verify_secret(x_seedlab_secret: Optional[str] = Header(default=None)) -> None:
    expected = str(settings.seedlab_eval_shared_secret or "").strip()
    if not expected or not x_seedlab_secret or not secrets.compare_digest(x_seedlab_secret, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-Seedlab-Secret header")


AuthDep = Depends(verify_secret)


@app.on_event("startup")
async def startup_event() -> None:
    _start_runtime_warmup()


def _evaluate_sync(body: EvaluateRequest) -> dict[str, Any]:
    _assert_runtime_ready()
    asr_api_key, judge_api_key = _resolve_openai_keys(
        explicit_shared_key="",
        explicit_asr_key="",
        explicit_judge_key="",
    )
    asr_model_resolved, _warning = _resolve_asr_model_for_transcription(body.asr_model or settings.seedlab_asr_model)
    judge_model = str(body.judge_model or settings.seedlab_judge_model).strip() or settings.seedlab_judge_model
    with tempfile.TemporaryDirectory(prefix="seedlab-eval-") as tmp:
        run_dir = Path(tmp)
        audio_path = _download_audio_to_temp(body.audio_s3_uri, run_dir)
        sample_id, evaluation, debug = _auto_eval_audio_file(
            run_dir=run_dir,
            sample_id=str(body.sample_id or "").strip(),
            seed=int(body.seed or 0),
            script_id=str(body.script_id or "").strip(),
            script_text=str(body.script_text or ""),
            audio_path=audio_path,
            rec={"audio_s3_uri": body.audio_s3_uri, "tts_params": {}},
            asr_api_key=asr_api_key,
            judge_api_key=judge_api_key,
            asr_model=asr_model_resolved,
            judge_model=judge_model,
            language=str(body.language or settings.seedlab_language).strip() or settings.seedlab_language,
            timeout_seconds=max(10, int(body.timeout_seconds or settings.seedlab_auto_eval_timeout)),
            evaluation_profile=str(body.evaluation_profile or settings.seedlab_evaluation_profile).strip() or settings.seedlab_evaluation_profile,
            reference_audio_local_path=settings.seedlab_reference_audio_local_path,
            reference_audio_s3_uri=settings.seedlab_reference_audio_s3_uri,
            reference_audio_cache_dir=settings.seedlab_reference_audio_cache_dir,
            disable_llm_note=bool(body.disable_llm_note),
            executor="runpod_gpu",
        )
    evaluation["executor"] = str(evaluation.get("executor") or "runpod_gpu")
    debug["executor"] = str(debug.get("executor") or "runpod_gpu")
    remote_eval = debug.get("remote_eval") if isinstance(debug.get("remote_eval"), dict) else {}
    remote_eval.update(
        {
            "executor": "runpod_gpu",
            "gpu_acceleration_active": bool(debug.get("gpu_acceleration_active")),
            "status": "completed",
        }
    )
    debug["remote_eval"] = remote_eval
    return {"ok": True, "sample_id": sample_id, "evaluation": evaluation, "debug": debug, "status": "completed"}


def _run_job(job_id: str, body: EvaluateRequest) -> None:
    current = _read_job(job_id)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _write_job(job_id, {**current, "status": "running", "error": "", "started_at": started_at})
    try:
        result = _evaluate_sync(body)
    except Exception as e:
        logger.exception("seedlab eval job failed job_id=%s", job_id)
        _write_job(
            job_id,
            {
                **current,
                "status": "failed",
                "error": str(e),
                "sample_id": str(body.sample_id or "").strip(),
                "started_at": started_at,
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        return
    debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
    remote_eval = debug.get("remote_eval") if isinstance(debug.get("remote_eval"), dict) else {}
    remote_eval.update(
        {
            "job_id": job_id,
            "submitted_at": str(current.get("queued_at") or current.get("created_at") or ""),
            "started_at": started_at,
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "completed",
        }
    )
    debug["remote_eval"] = remote_eval
    result["debug"] = debug
    _write_job(job_id, {**current, **result, "status": "completed", "error": "", "started_at": started_at, "finished_at": remote_eval["completed_at"]})


@app.post("/evaluate")
async def evaluate(body: EvaluateRequest, _: Any = AuthDep) -> dict[str, Any]:
    return _evaluate_sync(body)


@app.post("/evaluate/jobs")
async def evaluate_job_submit(body: EvaluateRequest, background_tasks: BackgroundTasks, _: Any = AuthDep) -> dict[str, Any]:
    _assert_runtime_ready()
    job_id = uuid.uuid4().hex
    queued_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _write_job(
        job_id,
        {
            "ok": True,
            "status": "queued",
            "sample_id": str(body.sample_id or "").strip(),
            "error": "",
            "queued_at": queued_at,
        },
    )
    background_tasks.add_task(_run_job, job_id, body)
    return {"ok": True, "job_id": job_id, "status": "queued", "sample_id": str(body.sample_id or "").strip()}


@app.get("/evaluate/jobs/{job_id}")
async def evaluate_job_status(job_id: str, _: Any = AuthDep) -> dict[str, Any]:
    return _read_job(job_id)


@app.get("/health")
async def health() -> dict[str, Any]:
    capabilities = _seedlab_runtime_capabilities(
        reference_audio_local_path=settings.seedlab_reference_audio_local_path,
        reference_audio_s3_uri=settings.seedlab_reference_audio_s3_uri,
    )
    capabilities["reference_count"] = max(
        int(capabilities.get("reference_count") or 0),
        int(_WARMUP_STATE.get("reference_count_loaded") or 0),
    )
    capabilities["mos_device"] = str(_WARMUP_STATE.get("mos_device") or capabilities.get("mos_device") or "")
    capabilities["speaker_device"] = str(_WARMUP_STATE.get("speaker_device") or capabilities.get("speaker_device") or "")
    capabilities["gpu_acceleration_active"] = bool(_WARMUP_STATE.get("gpu_ready"))
    return {
        "ok": str(_WARMUP_STATE.get("status") or "") == "ready",
        "eval_mode": "runpod_pod",
        "job_api_enabled": True,
        "job_dir": str(_job_dir()),
        "capabilities": capabilities,
        "reference_audio_local_path_configured": bool((settings.seedlab_reference_audio_local_path or "").strip()),
        "reference_audio_s3_uri_configured": bool((settings.seedlab_reference_audio_s3_uri or "").strip()),
        "warmup_status": str(_WARMUP_STATE.get("status") or "pending"),
        "warmup_error": str(_WARMUP_STATE.get("error") or ""),
        "reference_count_loaded": int(_WARMUP_STATE.get("reference_count_loaded") or 0),
        "reference_audio_source": str(_WARMUP_STATE.get("reference_audio_source") or ""),
        "reference_set_id": str(_WARMUP_STATE.get("reference_set_id") or ""),
        "mos_device": str(_WARMUP_STATE.get("mos_device") or ""),
        "speaker_device": str(_WARMUP_STATE.get("speaker_device") or ""),
        "gpu_ready": bool(_WARMUP_STATE.get("gpu_ready")),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.seedlab_eval_port)
