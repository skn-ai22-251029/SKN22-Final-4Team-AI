from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import importlib.util
import json
import logging
import secrets
import sys
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

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

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()


class RunCreateRequest(BaseModel):
    run_id: str = ""
    seeds: str = ""
    dup: bool = False
    dataset_path: str = ""


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
        "network_fail_count": int(state.get("network_fail_count") or 0),
        "http_502_count": int(state.get("http_502_count") or 0),
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


async def _notify_gateway_progress(state: dict[str, Any]) -> None:
    gateway_url = (settings.seedlab_gateway_url or "").strip().rstrip("/")
    secret = (settings.gateway_internal_secret or "").strip()
    if not gateway_url or not secret:
        return
    payload = {
        "run_id": str(state.get("run_id") or ""),
        "status": str(state.get("status") or ""),
        "stage": str(state.get("stage") or ""),
        "generated_count": int(state.get("generated_count") or 0),
        "evaluated_count": int(state.get("evaluated_count") or 0),
        "ready_count": int(state.get("ready_count") or 0),
        "failed_count": int(state.get("failed_count") or 0),
        "total_count": int(state.get("total_count") or 0),
        "last_error": str(state.get("last_error") or ""),
        "finished_at": str(state.get("finished_at") or ""),
    }

    def _post() -> None:
        req = urllib.request.Request(
            f"{gateway_url}/internal/seedlab-progress",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Internal-Secret": secret},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

    try:
        await asyncio.to_thread(_post)
    except Exception as e:
        logger.warning("seedlab progress notify failed run_id=%s err=%s", payload["run_id"], e)


def _update_state(run_id: str, **kwargs: Any) -> dict[str, Any]:
    state = _load_state(run_id)
    state.update(kwargs)
    _save_state(state)
    return state


def _normalize_record_for_service(rec: dict[str, Any]) -> dict[str, Any]:
    out = _normalize_record_for_ui(rec)
    rel = str(out.get("audio_rel_path") or "").strip().replace("\\", "/").lstrip("/")
    if rel:
        out["audio_url"] = urllib.parse.quote(rel, safe="/-_.~")
    return out


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
    try:
        _resolve_openai_keys(explicit_shared_key="", explicit_asr_key="", explicit_judge_key="")
        return True
    except Exception:
        return False


def _seedlab_capabilities() -> dict[str, Any]:
    advanced_dsp_enabled = all(importlib.util.find_spec(name) is not None for name in ("numpy", "scipy", "librosa", "soundfile"))
    mos_enabled = importlib.util.find_spec("distillmos") is not None
    speaker_similarity_enabled = (
        importlib.util.find_spec("speechbrain") is not None
        and importlib.util.find_spec("torch") is not None
        and importlib.util.find_spec("torchaudio") is not None
    )
    reference_corpus_loaded = bool((settings.seedlab_reference_audio_local_path or "").strip() or (settings.seedlab_reference_audio_s3_uri or "").strip())
    intonation_enabled = advanced_dsp_enabled and reference_corpus_loaded
    return {
        "advanced_dsp_enabled": advanced_dsp_enabled,
        "mos_enabled": mos_enabled,
        "speaker_similarity_enabled": speaker_similarity_enabled,
        "reference_corpus_loaded": reference_corpus_loaded,
        "intonation_enabled": intonation_enabled,
        "reference_audio_local_path_configured": bool((settings.seedlab_reference_audio_local_path or "").strip()),
        "reference_audio_s3_uri_configured": bool((settings.seedlab_reference_audio_s3_uri or "").strip()),
    }


async def _run_generation_pipeline(run_id: str) -> None:
    state = _load_state(run_id)
    run_dir = _run_dir(run_id)
    dataset_path = Path(str(state["dataset_path"])).resolve()
    scripts, tts_params = load_dataset(dataset_path)
    endpoint = _resolve_api_endpoint(state.get("api_endpoint") or settings.tts_api_url)
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
                from seed_lab import _append_jsonl_object  # type: ignore

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
    _write_manifest(run_dir, _meta_from_state(state), records)
    _generate_review_html(run_id, records, run_dir / "index.html")

    if _openai_enabled() and records:
        state = _update_state(run_id, status="auto_evaluating", stage="auto_evaluating", evaluated_count=0)
        await _notify_gateway_progress(state)
        asr_api_key, judge_api_key = _resolve_openai_keys(
            explicit_shared_key="",
            explicit_asr_key="",
            explicit_judge_key="",
        )
        asr_model_resolved, _asr_warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
        eval_out = run_dir / "auto_eval.json"
        eval_out.unlink(missing_ok=True)
        _live_eval_debug_path(run_id).unlink(missing_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [
                pool.submit(
                    _auto_eval_single_record,
                    run_dir=run_dir,
                    rec=rec,
                    asr_api_key=asr_api_key,
                    judge_api_key=judge_api_key,
                    asr_model=asr_model_resolved,
                    judge_model=settings.seedlab_judge_model,
                    language=settings.seedlab_language,
                    timeout_seconds=int(settings.seedlab_auto_eval_timeout),
                    evaluation_profile=settings.seedlab_evaluation_profile,
                    reference_audio_local_path=settings.seedlab_reference_audio_local_path,
                    reference_audio_s3_uri=settings.seedlab_reference_audio_s3_uri,
                    reference_audio_cache_dir=settings.seedlab_reference_audio_cache_dir,
                    disable_llm_note=bool(settings.seedlab_disable_llm_note),
                )
                for rec in records
                if rec.get("status") in ("ready", "skipped_existing")
            ]
            eval_done = 0
            for fut in concurrent.futures.as_completed(futures):
                sample_id, eval_obj, debug_obj = fut.result()
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
                state["evaluated_count"] = eval_done
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
        "api_endpoint": _resolve_api_endpoint(settings.tts_api_url),
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
        "ready_count": 0,
        "total_count": requested_samples * takes_per_seed * len(selected_scripts),
        "last_error": "",
        "created_at": _utcnow().isoformat(),
        "started_at": "",
        "finished_at": "",
    }
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
        "tts_endpoint": _resolve_api_endpoint(settings.tts_api_url),
        "default_tts_params": default_tts_params,
        "auto_eval_on_add": _openai_enabled(),
        "openai_configured": _openai_enabled(),
        "asr_model": asr_model_resolved,
        "asr_model_requested": settings.seedlab_asr_model,
        "judge_model": settings.seedlab_judge_model,
        "evaluation_profile": settings.seedlab_evaluation_profile,
        "asr_warning": asr_warning,
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
    endpoint = _resolve_api_endpoint(settings.tts_api_url)
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
    if _to_bool(body.get("add_to_review"), default=False):
        from seed_lab import _append_jsonl_object  # type: ignore

        _append_jsonl_object(run_dir / LIVE_RECORDS_JSONL, rec)
    ai_eval_obj = None
    if _to_bool(body.get("add_to_review"), default=False) and _openai_enabled():
        asr_api_key, judge_api_key = _resolve_openai_keys(explicit_shared_key="", explicit_asr_key="", explicit_judge_key="")
        asr_model_resolved, _warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
        sample_id_eval, eval_obj, debug_obj = await asyncio.to_thread(
            _auto_eval_single_record,
            run_dir=run_dir,
            rec=rec,
            asr_api_key=asr_api_key,
            judge_api_key=judge_api_key,
            asr_model=asr_model_resolved,
            judge_model=settings.seedlab_judge_model,
            language=settings.seedlab_language,
            timeout_seconds=int(settings.seedlab_auto_eval_timeout),
            evaluation_profile=settings.seedlab_evaluation_profile,
            reference_audio_local_path=settings.seedlab_reference_audio_local_path,
            reference_audio_s3_uri=settings.seedlab_reference_audio_s3_uri,
            reference_audio_cache_dir=settings.seedlab_reference_audio_cache_dir,
            disable_llm_note=bool(settings.seedlab_disable_llm_note),
        )
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
    asr_api_key, judge_api_key = _resolve_openai_keys(explicit_shared_key="", explicit_asr_key="", explicit_judge_key="")
    asr_model_resolved, _warning = _resolve_asr_model_for_transcription(settings.seedlab_asr_model)
    sample_id_eval, eval_obj, debug_obj = await asyncio.to_thread(
        _auto_eval_single_record,
        run_dir=run_dir,
        rec=rec,
        asr_api_key=asr_api_key,
        judge_api_key=judge_api_key,
        asr_model=asr_model_resolved,
        judge_model=settings.seedlab_judge_model,
        language=settings.seedlab_language,
        timeout_seconds=int(settings.seedlab_auto_eval_timeout),
        evaluation_profile=settings.seedlab_evaluation_profile,
        reference_audio_local_path=settings.seedlab_reference_audio_local_path,
        reference_audio_s3_uri=settings.seedlab_reference_audio_s3_uri,
        reference_audio_cache_dir=settings.seedlab_reference_audio_cache_dir,
        disable_llm_note=bool(settings.seedlab_disable_llm_note),
    )
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
