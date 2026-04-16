from __future__ import annotations

import logging
import secrets
import sys
import tempfile
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
    _resolve_asr_model_for_transcription,
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


async def verify_secret(x_seedlab_secret: Optional[str] = Header(default=None)) -> None:
    expected = str(settings.seedlab_eval_shared_secret or "").strip()
    if not expected or not x_seedlab_secret or not secrets.compare_digest(x_seedlab_secret, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-Seedlab-Secret header")


AuthDep = Depends(verify_secret)


def _evaluate_sync(body: EvaluateRequest) -> dict[str, Any]:
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
        )
    return {"ok": True, "sample_id": sample_id, "evaluation": evaluation, "debug": debug, "status": "completed"}


def _run_job(job_id: str, body: EvaluateRequest) -> None:
    current = _read_job(job_id)
    _write_job(job_id, {**current, "status": "running", "error": ""})
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
            },
        )
        return
    _write_job(job_id, {**current, **result, "status": "completed", "error": ""})


@app.post("/evaluate")
async def evaluate(body: EvaluateRequest, _: Any = AuthDep) -> dict[str, Any]:
    return _evaluate_sync(body)


@app.post("/evaluate/jobs")
async def evaluate_job_submit(body: EvaluateRequest, background_tasks: BackgroundTasks, _: Any = AuthDep) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    _write_job(
        job_id,
        {
            "ok": True,
            "status": "queued",
            "sample_id": str(body.sample_id or "").strip(),
            "error": "",
        },
    )
    background_tasks.add_task(_run_job, job_id, body)
    return {"ok": True, "job_id": job_id, "status": "queued", "sample_id": str(body.sample_id or "").strip()}


@app.get("/evaluate/jobs/{job_id}")
async def evaluate_job_status(job_id: str, _: Any = AuthDep) -> dict[str, Any]:
    return _read_job(job_id)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "eval_mode": "runpod_pod",
        "job_api_enabled": True,
        "job_dir": str(_job_dir()),
        "capabilities": _seedlab_runtime_capabilities(
            reference_audio_local_path=settings.seedlab_reference_audio_local_path,
            reference_audio_s3_uri=settings.seedlab_reference_audio_s3_uri,
        ),
        "reference_audio_local_path_configured": bool((settings.seedlab_reference_audio_local_path or "").strip()),
        "reference_audio_s3_uri_configured": bool((settings.seedlab_reference_audio_s3_uri or "").strip()),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.seedlab_eval_port)
