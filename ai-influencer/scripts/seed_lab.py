#!/usr/bin/env python3
"""
RunPod TTS seed exploration lab.

Goals:
- Generate many random-seed TTS samples locally.
- Provide a static HTML review UI (audio player + memo + scoring).
- Export/import reviewer evaluations as JSON.
- Build ranked seed report and stage-B seed list.
"""

from __future__ import annotations

import argparse
import audioop
import concurrent.futures
import csv
import datetime as dt
import hashlib
import html
import http.server
import importlib.util
import json
import math
import mimetypes
import os
import random
import re
import socketserver
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SEED_MAX = 2_147_483_647
DEFAULT_OUTPUT_ROOT = "seed-lab-runs"
DEFAULT_CONCURRENCY = 2
DEFAULT_TIMEOUT = 300
DEFAULT_SAMPLES = 20
DEFAULT_STAGE_B_TOP = 20
DEFAULT_TAKES_PER_SEED = 1
DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8765
DEFAULT_AUTO_EVAL_ASR_MODEL = "gpt-4o-transcribe"
DEFAULT_AUTO_EVAL_JUDGE_MODEL = "gpt-5.4"
DEFAULT_AUTO_EVAL_TIMEOUT = 120
DEFAULT_AUTO_EVAL_PROFILE = "hybrid"
DEFAULT_SEEDLAB_EVAL_DEVICE = "auto"
DEFAULT_SEEDLAB_EVAL_MODEL_CACHE_DIR = "/workspace/runpod-stack/cache/seedlab-models"
LIVE_RECORDS_JSONL = "live_records.jsonl"
LIVE_AUTO_EVAL_JSON = "auto_eval_live.json"
HUMAN_EVAL_JSON = "human_eval.json"
AI_SCORE_KEYS = (
    "naturalness",
    "pronunciation",
    "stability",
    "tone_fit",
    "pitch_consistency",
    "artifact_cleanliness",
    "intonation_similarity",
)
_DISTILLMOS_PREDICTORS: dict[str, Any] = {}
_SPEAKER_VERIFIERS: dict[str, Any] = {}
_TORCH_RUNTIME: dict[str, Any] | None = None
_REFERENCE_CORPUS_SUMMARIES: dict[str, dict[str, Any]] = {}
_REFERENCE_SPEAKER_EMBEDDINGS: dict[tuple[str, str], Any] = {}
_REFERENCE_CACHE_LOCK = threading.Lock()


@dataclass
class ScriptItem:
    script_id: str
    title: str
    text: str


def _now_compact() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_slug(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "item"
    keep = []
    for ch in raw:
        if ch.isalnum() or ch in ("-", "_"):
            keep.append(ch)
        else:
            keep.append("-")
    slug = "".join(keep).strip("-")
    return slug or "item"


def _seedlab_eval_model_cache_dir() -> Path:
    raw = (os.getenv("SEEDLAB_EVAL_MODEL_CACHE_DIR") or DEFAULT_SEEDLAB_EVAL_MODEL_CACHE_DIR).strip()
    path = Path(raw or DEFAULT_SEEDLAB_EVAL_MODEL_CACHE_DIR).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        fallback = Path("/tmp/seedlab-model-cache").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return path.resolve()


def _path_cache_signature(path: Path) -> str:
    resolved = path.resolve()
    try:
        stat = resolved.stat()
        return f"{resolved}:{int(stat.st_mtime_ns)}:{int(stat.st_size)}"
    except Exception:
        return str(resolved)


def _reference_paths_cache_key(reference_audio_paths: list[Path]) -> str:
    signature = "|".join(_path_cache_signature(path) for path in reference_audio_paths if path.exists())
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _get_torch_runtime() -> dict[str, Any]:
    global _TORCH_RUNTIME
    if _TORCH_RUNTIME is not None:
        return dict(_TORCH_RUNTIME)
    info: dict[str, Any] = {
        "torch_available": False,
        "torch_version": "",
        "cuda_available": False,
        "cuda_device_count": 0,
        "cuda_version": "",
        "cuda_device_name": "",
        "runtime_error": "",
    }
    try:
        import torch  # type: ignore

        info["torch_available"] = True
        info["torch_version"] = str(getattr(torch, "__version__", "") or "")
        info["cuda_version"] = str(getattr(torch.version, "cuda", "") or "")
        cuda_available = bool(torch.cuda.is_available())
        info["cuda_available"] = cuda_available
        if cuda_available:
            try:
                info["cuda_device_count"] = int(torch.cuda.device_count())
            except Exception:
                info["cuda_device_count"] = 0
            try:
                info["cuda_device_name"] = str(torch.cuda.get_device_name(0) or "")
            except Exception:
                info["cuda_device_name"] = ""
    except Exception as e:
        info["runtime_error"] = str(e)
    _TORCH_RUNTIME = dict(info)
    return dict(info)


def _resolve_seedlab_eval_runtime() -> dict[str, Any]:
    requested_raw = (os.getenv("SEEDLAB_EVAL_DEVICE") or DEFAULT_SEEDLAB_EVAL_DEVICE).strip().lower()
    requested = requested_raw or DEFAULT_SEEDLAB_EVAL_DEVICE
    if requested != "cpu" and requested != "auto" and not requested.startswith("cuda"):
        requested = DEFAULT_SEEDLAB_EVAL_DEVICE
    require_gpu = _to_bool(os.getenv("SEEDLAB_EVAL_REQUIRE_GPU"), False)
    torch_runtime = _get_torch_runtime()
    cuda_available = bool(torch_runtime.get("cuda_available"))
    resolved = "cpu"
    fallback_reason = ""
    if requested == "cpu":
        resolved = "cpu"
    elif requested == "auto":
        if cuda_available:
            resolved = "cuda:0"
        else:
            resolved = "cpu"
            fallback_reason = "cuda unavailable"
    else:
        if cuda_available:
            resolved = requested if ":" in requested else "cuda:0"
        else:
            resolved = "cpu"
            fallback_reason = f"requested {requested} but cuda unavailable"
    if require_gpu and not resolved.startswith("cuda"):
        raise RuntimeError(f"SEEDLAB_EVAL_REQUIRE_GPU=true but GPU runtime is unavailable ({fallback_reason or 'resolved to cpu'})")
    return {
        **torch_runtime,
        "requested_device": requested_raw or DEFAULT_SEEDLAB_EVAL_DEVICE,
        "resolved_device": resolved,
        "require_gpu": require_gpu,
        "gpu_acceleration_active": False,
        "fallback_reason": fallback_reason,
        "model_cache_dir": str(_seedlab_eval_model_cache_dir()),
    }


def _torch_device_is_cuda(device: str) -> bool:
    return str(device or "").strip().lower().startswith("cuda")


def _infer_module_device(obj: Any) -> str:
    seen: set[int] = set()
    queue: list[Any] = [obj]
    attr_names = (
        "device",
        "module",
        "model",
        "_model",
        "net",
        "mods",
        "modules",
        "embedding_model",
        "classifier",
        "encoder",
        "pipeline",
    )
    while queue:
        current = queue.pop(0)
        if current is None:
            continue
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        try:
            device_attr = getattr(current, "device", None)
            if device_attr is not None:
                text = str(device_attr).strip()
                if text:
                    return text
        except Exception:
            pass
        try:
            parameters = getattr(current, "parameters", None)
            if callable(parameters):
                first_param = next(parameters(), None)
                if first_param is not None:
                    return str(first_param.device)
        except Exception:
            pass
        for name in attr_names:
            try:
                child = getattr(current, name, None)
            except Exception:
                continue
            if child is None:
                continue
            if isinstance(child, dict):
                queue.extend(child.values())
            elif isinstance(child, (list, tuple, set)):
                queue.extend(list(child))
            else:
                queue.append(child)
    return ""


def _load_distillmos_predictor(resolved_device: str) -> Any:
    global _DISTILLMOS_PREDICTORS
    predictor = _DISTILLMOS_PREDICTORS.get(resolved_device)
    if predictor is not None:
        return predictor

    import distillmos  # type: ignore

    model_cls = getattr(distillmos, "ConvTransformerSQAModel", None)
    if model_cls is None:
        raise RuntimeError("distillmos.ConvTransformerSQAModel is unavailable")

    model = model_cls()
    if hasattr(model, "to"):
        model.to(resolved_device)
    if hasattr(model, "eval"):
        model.eval()
    _DISTILLMOS_PREDICTORS[resolved_device] = model
    return model


def _load_speaker_verifier(resolved_device: str) -> Any:
    global _SPEAKER_VERIFIERS
    verifier = _SPEAKER_VERIFIERS.get(resolved_device)
    if verifier is not None:
        return verifier

    from speechbrain.inference.speaker import SpeakerRecognition  # type: ignore

    verifier = SpeakerRecognition.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(_seedlab_eval_model_cache_dir() / "speechbrain"),
        run_opts={"device": resolved_device},
    )
    _SPEAKER_VERIFIERS[resolved_device] = verifier
    return verifier


def _download_s3_file_if_missing(s3: Any, bucket: str, key: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            if target.stat().st_size > 0:
                return
        except Exception:
            pass
    s3.download_file(bucket, key, str(target))


def _get_reference_corpus_summary(reference_audio_paths: list[Path]) -> dict[str, Any]:
    cache_key = _reference_paths_cache_key(reference_audio_paths)
    with _REFERENCE_CACHE_LOCK:
        cached = _REFERENCE_CORPUS_SUMMARIES.get(cache_key)
    if cached is not None:
        return cached
    summary = _build_reference_corpus_summary(reference_audio_paths)
    with _REFERENCE_CACHE_LOCK:
        _REFERENCE_CORPUS_SUMMARIES[cache_key] = summary
    return summary


def _load_reference_speaker_embeddings(reference_audio_paths: list[Path], resolved_device: str) -> tuple[Any, Any]:
    cache_key = (resolved_device, _reference_paths_cache_key(reference_audio_paths))
    with _REFERENCE_CACHE_LOCK:
        cached = _REFERENCE_SPEAKER_EMBEDDINGS.get(cache_key)
    verifier = _load_speaker_verifier(resolved_device)
    if cached is not None:
        return verifier, cached

    import torch  # type: ignore

    embeddings = []
    with torch.no_grad():
        for ref_path in reference_audio_paths:
            waveform = verifier.load_audio(str(ref_path))
            emb = verifier.encode_batch(waveform.unsqueeze(0), normalize=False).detach()
            embeddings.append(emb)
    if not embeddings:
        raise RuntimeError("reference speaker embeddings are unavailable")
    ref_embeddings = torch.cat(embeddings, dim=0)
    with _REFERENCE_CACHE_LOCK:
        _REFERENCE_SPEAKER_EMBEDDINGS[cache_key] = ref_embeddings
    return verifier, ref_embeddings


def _predict_speaker_similarity(
    *,
    audio_path: Path,
    reference_audio_paths: list[Path],
    resolved_device: str,
) -> tuple[float, str]:
    import torch  # type: ignore

    verifier, ref_embeddings = _load_reference_speaker_embeddings(reference_audio_paths, resolved_device)
    with torch.no_grad():
        waveform = verifier.load_audio(str(audio_path))
        sample_embedding = verifier.encode_batch(waveform.unsqueeze(0), normalize=False).detach()
        score = verifier.similarity(sample_embedding.expand_as(ref_embeddings), ref_embeddings)
    values = [float(item) for item in score.reshape(-1).detach().cpu().tolist()]
    device = _infer_module_device(verifier) or str(getattr(verifier, "device", "") or resolved_device)
    return (_pairwise_mean(values) or 0.0), device


def _predict_distillmos_mos(audio_path: Path, resolved_device: str) -> tuple[float, str]:
    runtime = _get_torch_runtime()
    if not runtime.get("torch_available"):
        raise RuntimeError("torch is unavailable")
    if importlib.util.find_spec("torchaudio") is None:
        raise RuntimeError("torchaudio is unavailable")

    import torch  # type: ignore
    import torchaudio  # type: ignore

    model = _load_distillmos_predictor(resolved_device)
    waveform, sample_rate = torchaudio.load(str(audio_path))
    if waveform.shape[0] > 1:
        waveform = waveform[:1, :]
    if sample_rate != 16000:
        waveform = torchaudio.transforms.Resample(sample_rate, 16000)(waveform)
    waveform = waveform.to(resolved_device)
    with torch.no_grad():
        score = model(waveform)
    value = float(score.reshape(-1)[0].item())
    device = _infer_module_device(model) or resolved_device
    return value, device


def _seedlab_runtime_capabilities(
    *,
    reference_audio_local_path: str = "",
    reference_audio_s3_uri: str = "",
    reference_audio_paths: list[Path] | None = None,
) -> dict[str, Any]:
    runtime = _resolve_seedlab_eval_runtime()
    reference_audio_paths = [p for p in (reference_audio_paths or []) if p.exists()]
    reference_corpus_loaded = bool(reference_audio_paths) or bool(reference_audio_local_path.strip() or reference_audio_s3_uri.strip())
    advanced_dsp_enabled = all(
        importlib.util.find_spec(name) is not None for name in ("numpy", "scipy", "librosa", "soundfile")
    )
    return {
        "advanced_dsp_enabled": advanced_dsp_enabled,
        "mos_dependency_installed": importlib.util.find_spec("distillmos") is not None,
        "speaker_dependency_installed": (
            importlib.util.find_spec("speechbrain") is not None
            and importlib.util.find_spec("torch") is not None
            and importlib.util.find_spec("torchaudio") is not None
        ),
        "mos_enabled": False,
        "speaker_similarity_enabled": False,
        "reference_corpus_loaded": reference_corpus_loaded,
        "reference_count": len(reference_audio_paths),
        "intonation_enabled": advanced_dsp_enabled and reference_corpus_loaded,
        "requested_device": runtime.get("requested_device"),
        "resolved_device": runtime.get("resolved_device"),
        "require_gpu": runtime.get("require_gpu"),
        "cuda_available": runtime.get("cuda_available"),
        "cuda_device_count": runtime.get("cuda_device_count"),
        "cuda_device_name": runtime.get("cuda_device_name"),
        "cuda_version": runtime.get("cuda_version"),
        "torch_available": runtime.get("torch_available"),
        "torch_version": runtime.get("torch_version"),
        "gpu_acceleration_active": False,
        "fallback_reason": runtime.get("fallback_reason"),
        "model_cache_dir": runtime.get("model_cache_dir"),
        "mos_device": "",
        "speaker_device": "",
    }


def _load_optional_yaml(text: str, path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError(
            f"YAML parsing requires PyYAML. Install it or use JSON dataset. file={path} err={e}"
        ) from e
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"dataset must be an object: {path}")
    return parsed


def _resolve_seedlab_base_script_override() -> str:
    raw = (os.getenv("SEEDLAB_BASE_SCRIPT_TEXT") or "").strip()
    if not raw:
        return ""
    return raw.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\r")


def load_dataset(path: Path) -> tuple[list[ScriptItem], dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"dataset not found: {path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        raw = json.loads(text)
    else:
        raw = _load_optional_yaml(text, path)
    if not isinstance(raw, dict):
        raise RuntimeError(f"dataset root must be object: {path}")

    scripts_raw = raw.get("scripts")
    if not isinstance(scripts_raw, list) or not scripts_raw:
        raise RuntimeError("dataset.scripts must be a non-empty list")

    scripts: list[ScriptItem] = []
    base_script_override = _resolve_seedlab_base_script_override()
    for idx, item in enumerate(scripts_raw, start=1):
        if isinstance(item, str):
            script_id = f"s{idx}"
            title = f"Script {idx}"
            content = item.strip()
        elif isinstance(item, dict):
            script_id = _safe_slug(str(item.get("id") or f"s{idx}"))
            title = str(item.get("title") or f"Script {idx}").strip()
            content = str(item.get("text") or "").strip()
        else:
            raise RuntimeError(f"dataset.scripts[{idx}] invalid type={type(item).__name__}")
        if base_script_override and script_id == "s1":
            content = base_script_override
        if not content:
            raise RuntimeError(f"dataset.scripts[{idx}] text is empty")
        scripts.append(ScriptItem(script_id=script_id, title=title, text=content))

    tts_params = raw.get("tts_params")
    if tts_params is None:
        tts_params = {}
    if not isinstance(tts_params, dict):
        raise RuntimeError("dataset.tts_params must be an object")
    return scripts, tts_params


def _resolve_api_endpoint(api_url: str) -> str:
    base = (api_url or "").strip()
    if not base:
        raise RuntimeError("api_url is required (or set TTS_API_URL)")
    return base if base.rstrip("/").endswith("/tts") else f"{base.rstrip('/')}/tts"


def _build_payload(script_text: str, tts_params: dict[str, Any], seed: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": script_text,
        "text_lang": tts_params.get("text_lang", "ko"),
        "prompt_lang": tts_params.get("prompt_lang", tts_params.get("text_lang", "ko")),
        "media_type": "wav",
        "streaming_mode": False,
        "top_k": int(tts_params.get("top_k", 20)),
        "sample_steps": int(tts_params.get("sample_steps", 32)),
        "super_sampling": bool(tts_params.get("super_sampling", True)),
        "fragment_interval": float(tts_params.get("fragment_interval", 0.4)),
        "seed": int(seed),
    }

    ref_audio_path = str(tts_params.get("ref_audio_path", "")).strip()
    prompt_text = str(tts_params.get("prompt_text", "")).strip()
    if ref_audio_path or prompt_text:
        if not (ref_audio_path and prompt_text):
            raise RuntimeError("tts_params.ref_audio_path and tts_params.prompt_text must be both set")
        payload["ref_audio_path"] = ref_audio_path
        payload["prompt_text"] = prompt_text
    return payload


def _http_post_tts(endpoint: str, payload: dict[str, Any], timeout_seconds: int) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {body[:800]}") from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _parse_script_ids(raw: str) -> list[str]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return [_safe_slug(p) for p in parts]


def _parse_seed_values(raw: str) -> list[int]:
    values = [token.strip() for token in (raw or "").split(",") if token.strip()]
    seeds: list[int] = []
    for token in values:
        seed = int(token)
        if seed <= 0 or seed > SEED_MAX:
            raise RuntimeError(f"seed out of range: {seed}")
        if seed in seeds:
            continue
        seeds.append(seed)
    return seeds


def _load_seeds_file(path: Path) -> list[int]:
    if not path.exists():
        raise RuntimeError(f"seeds file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            candidates = parsed.get("seeds") or parsed.get("top_seeds") or []
        else:
            candidates = parsed
        if not isinstance(candidates, list):
            raise RuntimeError(f"invalid seeds json: {path}")
        raw = ",".join(str(v) for v in candidates)
        return _parse_seed_values(raw)
    raw = text.replace("\n", ",")
    return _parse_seed_values(raw)


def _random_unique_seeds(count: int, exclude: set[int] | None = None) -> list[int]:
    if count <= 0:
        raise RuntimeError("samples must be > 0")
    blocked = set(exclude or set())
    seeds: list[int] = []
    while len(seeds) < count:
        seed = random.randint(1, SEED_MAX)
        if seed in blocked:
            continue
        if seed not in seeds:
            seeds.append(seed)
    return seeds


def _is_network_origin_error(detail: str) -> bool:
    normalized = (detail or "").lower()
    markers = (
        "http 502",
        "bad gateway",
        "unable to reach the origin service",
        "connection reset by peer",
        "incoming request ended abruptly",
        "temporarily unavailable",
        "timed out",
        "temporary failure in name resolution",
        "name or service not known",
        "connecterror",
        "readtimeout",
        "connecttimeout",
    )
    return any(marker in normalized for marker in markers)


def _unique_preserve_order(values: list[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _expand_seed_list_with_random(user_seeds: list[int], samples: int) -> tuple[list[int], int, int]:
    if samples <= 0:
        raise RuntimeError("samples must be > 0")
    base = _unique_preserve_order(user_seeds)
    if len(base) >= samples:
        truncated = len(base) - samples
        return base[:samples], 0, truncated
    needed = samples - len(base)
    extra = _random_unique_seeds(needed, exclude=set(base))
    return base + extra, needed, 0


def _pick_scripts_for_stage(
    scripts: list[ScriptItem],
    stage: str,
    script_ids: list[str],
) -> list[ScriptItem]:
    by_id = {s.script_id: s for s in scripts}
    if script_ids:
        selected: list[ScriptItem] = []
        for sid in script_ids:
            if sid not in by_id:
                raise RuntimeError(f"script id not found in dataset: {sid}")
            selected.append(by_id[sid])
        return selected

    if stage == "a":
        return [scripts[0]]
    if stage == "b":
        if len(scripts) < 3:
            raise RuntimeError("stage b needs at least 3 scripts in dataset")
        return [scripts[1], scripts[2]]
    return scripts


def _build_run_id(stage: str) -> str:
    return f"{_now_compact()}-{stage}-{uuid.uuid4().hex[:6]}"


def _load_existing_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    records = parsed.get("records")
    if not isinstance(records, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for rec in records:
        if isinstance(rec, dict):
            key = str(rec.get("sample_id") or "").strip()
            if key:
                out[key] = rec
    return out


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            raw = line.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def _append_jsonl_object(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _load_eval_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"evaluations": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"evaluations": {}}
    if not isinstance(parsed, dict):
        return {"evaluations": {}}
    if not isinstance(parsed.get("evaluations"), dict):
        parsed["evaluations"] = {}
    return parsed


def _merge_eval_maps(paths: list[Path]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = _load_eval_payload(path)
        evaluations = payload.get("evaluations")
        if not isinstance(evaluations, dict):
            continue
        for sample_id, eval_obj in evaluations.items():
            if isinstance(eval_obj, dict):
                merged[str(sample_id)] = eval_obj
    return merged


def _upsert_eval_entry(
    path: Path,
    *,
    run_id: str,
    sample_id: str,
    eval_obj: dict[str, Any],
    asr_model: str,
    judge_model: str,
) -> None:
    payload = _load_eval_payload(path)
    payload["run_id"] = str(payload.get("run_id") or run_id)
    payload["exported_at"] = dt.datetime.now().isoformat()
    payload["mode"] = str(payload.get("mode") or "auto_eval_hybrid_v2")
    payload["asr_model"] = str(payload.get("asr_model") or asr_model)
    payload["judge_model"] = str(payload.get("judge_model") or judge_model)
    evaluations = payload.get("evaluations")
    if not isinstance(evaluations, dict):
        evaluations = {}
        payload["evaluations"] = evaluations
    evaluations[sample_id] = eval_obj
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_human_eval_map(path: Path) -> dict[str, dict[str, Any]]:
    payload = _load_eval_payload(path)
    evaluations = payload.get("evaluations")
    if not isinstance(evaluations, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sample_id, eval_obj in evaluations.items():
        if isinstance(eval_obj, dict):
            out[str(sample_id)] = eval_obj
    return out


def _write_human_eval_map(path: Path, *, run_id: str, evaluations: dict[str, Any]) -> None:
    payload = {
        "run_id": run_id,
        "exported_at": dt.datetime.now().isoformat(),
        "mode": "human_eval_v1",
        "evaluations": evaluations if isinstance(evaluations, dict) else {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "t", "yes", "y", "on"):
        return True
    if text in ("0", "false", "f", "no", "n", "off"):
        return False
    return default


def _to_int(
    value: Any,
    *,
    default: int,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    try:
        num = int(str(value).strip())
    except Exception:
        num = default
    if min_value is not None and num < min_value:
        num = min_value
    if max_value is not None and num > max_value:
        num = max_value
    return num


def _to_float(
    value: Any,
    *,
    default: float,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    try:
        num = float(str(value).strip())
    except Exception:
        num = default
    if min_value is not None and num < min_value:
        num = min_value
    if max_value is not None and num > max_value:
        num = max_value
    return num


def _audio_url_from_rel_path(rel_path: str) -> str:
    cleaned = rel_path.strip().replace("\\", "/").lstrip("/")
    return "/" + urllib.parse.quote(cleaned, safe="/-_.~")


def _generate_review_html(run_id: str, records: list[dict[str, Any]], html_path: Path) -> None:
    manifest_json = json.dumps(records, ensure_ascii=False)
    safe_run_id = html.escape(run_id)
    html_body = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Seed Lab Review - {safe_run_id}</title>
  <style>
    :root {{
      --bg: #f3f7fb;
      --card: #ffffff;
      --line: #d7e1ea;
      --text: #102a43;
      --muted: #5c6f82;
      --accent: #1273de;
    }}
    body {{
      margin: 0;
      font-family: "Pretendard", "Noto Sans KR", sans-serif;
      color: var(--text);
      background: linear-gradient(135deg, #f8fbff, #eef4f9);
    }}
    .wrap {{
      width: 100%;
      margin: 0;
      padding: clamp(8px, 1.2vw, 14px);
      box-sizing: border-box;
    }}
    .panel {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      margin-bottom: 10px;
      box-shadow: 0 4px 14px rgba(16, 42, 67, 0.06);
    }}
    .row {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      align-items: center;
    }}
    label {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 12px;
      line-height: 1.2;
    }}
    input, select, button {{
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 6px 8px;
      background: #fff;
      color: var(--text);
      font-size: 12px;
      line-height: 1.2;
    }}
    button {{
      cursor: pointer;
    }}
    button.primary {{
      background: var(--accent);
      color: #fff;
      border: 1px solid var(--accent);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 11px;
      table-layout: auto;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 4px 6px;
      vertical-align: top;
      text-align: left;
      line-height: 1.25;
      word-wrap: break-word;
    }}
    th {{
      background: #f6f9fc;
      position: sticky;
      top: 0;
      z-index: 5;
    }}
    th.sortable {{
      cursor: pointer;
      user-select: none;
    }}
    td audio {{
      width: 180px;
      height: 28px;
    }}
    .memo {{
      width: 100%;
      min-height: 44px;
    }}
    .muted {{
      color: var(--muted);
      font-size: 11px;
    }}
    .script-btn {{
      margin-top: 4px;
      padding: 4px 6px;
      font-size: 11px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #f6f9fc;
      color: var(--text);
      cursor: pointer;
    }}
    #seedFilter {{
      flex: 1 1 140px;
      min-width: 120px;
    }}
    #scriptFilter {{
      flex: 1 1 180px;
      min-width: 140px;
    }}
    #statusFilter {{
      flex: 0 1 130px;
      min-width: 100px;
    }}
    #importFile {{
      flex: 1 1 260px;
      min-width: 180px;
    }}
    @media (max-width: 1024px) {{
      .wrap {{
        padding: 8px;
      }}
      .panel {{
        padding: 8px;
        margin-bottom: 8px;
      }}
      th, td {{
        padding: 3px 5px;
      }}
      td audio {{
        width: 150px;
      }}
    }}
    @media (max-width: 768px) {{
      table {{
        font-size: 10.5px;
      }}
      input, select, button {{
        font-size: 11px;
        padding: 5px 7px;
      }}
    }}
    .modal {{
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.45);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 9999;
      padding: 20px;
    }}
    .modal-card {{
      width: min(900px, 94vw);
      max-height: 84vh;
      overflow: auto;
      background: #fff;
      border-radius: 12px;
      border: 1px solid var(--line);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
      padding: 16px;
    }}
    .modal-title {{
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .modal-pre {{
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 13px;
      line-height: 1.55;
      background: #f8fbff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }}
    .ok {{
      color: #16794b;
      font-weight: 600;
    }}
    .bad {{
      color: #b42318;
      font-weight: 600;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <h2 style="margin:0 0 10px 0;">Seed Lab Review - {safe_run_id}</h2>
      <div class="row">
        <input id="seedFilter" placeholder="seed 필터 (부분검색)" />
        <input id="scriptFilter" placeholder="script_id 필터" />
        <select id="statusFilter">
          <option value="">상태 전체</option>
          <option value="ready">ready</option>
          <option value="skipped_existing">skipped_existing</option>
          <option value="failed">failed</option>
        </select>
        <label><input type="checkbox" id="selectedOnly" /> 선택한 후보만</label>
        <button id="resetBtn">필터 초기화</button>
      </div>
      <div class="row" style="margin-top:8px;">
        <button class="primary" id="exportBtn">평가 Export(JSON)</button>
        <input id="importFile" type="file" accept=".json,application/json" />
        <button id="importBtn">평가 Import</button>
        <button id="aiExportBtn">AI 평가 Export(JSON)</button>
        <span class="muted">저장 위치: localStorage (브라우저)</span>
      </div>
      <div class="row" style="margin-top:8px;">
        <span id="summary" class="muted"></span>
      </div>
      <div class="row" style="margin-top:6px;">
        <span id="aiHint" class="muted"></span>
      </div>
      <div class="row" style="margin-top:4px;">
        <span id="aiSummary" class="muted"></span>
      </div>
      <div class="row" style="margin-top:4px;">
        <span id="runStatusMeta" class="muted"></span>
      </div>
    </div>
    <div class="panel">
      <h3 style="margin:0 0 10px 0;">즉석 생성 테스트 (파라미터 조정)</h3>
      <div class="row">
        <label>seed <input id="pgSeed" placeholder="비우면 랜덤" style="width:160px;" /></label>
        <label>text_lang <input id="pgTextLang" value="ko" style="width:90px;" /></label>
        <label>prompt_lang <input id="pgPromptLang" value="ko" style="width:90px;" /></label>
        <label>top_k <input id="pgTopK" value="20" style="width:80px;" /></label>
        <label>sample_steps <input id="pgSampleSteps" value="32" style="width:90px;" /></label>
        <label>fragment_interval <input id="pgFragmentInterval" value="0.4" style="width:90px;" /></label>
        <label><input id="pgSuperSampling" type="checkbox" checked /> super_sampling</label>
        <label><input id="pgAddToReview" type="checkbox" /> 평가 테이블에 추가</label>
      </div>
      <div class="row" style="margin-top:8px;">
        <input id="pgRefAudioPath" placeholder="ref_audio_path (선택)" style="min-width:340px;flex:1;" />
        <input id="pgPromptText" placeholder="prompt_text (선택)" style="min-width:340px;flex:1;" />
      </div>
      <div class="row" style="margin-top:8px;">
        <textarea id="pgScriptText" class="memo" style="min-height:96px;" placeholder="샘플 텍스트를 입력하세요"></textarea>
      </div>
      <div class="row" style="margin-top:8px;">
        <button class="primary" id="pgGenerateBtn">TTS 생성</button>
        <span id="pgStatus" class="muted"></span>
      </div>
      <div class="row" style="margin-top:8px;">
        <audio id="pgAudio" controls preload="none" style="width:100%;max-width:520px;"></audio>
      </div>
    </div>
    <div class="panel" style="overflow:auto; max-height:70vh;">
      <table>
        <thead>
          <tr>
            <th class="sortable" data-human-sort="seed" data-label="seed" style="min-width:58px;">seed</th>
            <th class="sortable" data-human-sort="script_id" data-label="script_id" style="min-width:72px;">script_id</th>
            <th style="min-width:110px;">script</th>
            <th style="min-width:170px;">audio</th>
            <th class="sortable" data-human-sort="naturalness" data-label="자연" style="min-width:44px;">자연</th>
            <th class="sortable" data-human-sort="pronunciation" data-label="발음" style="min-width:44px;">발음</th>
            <th class="sortable" data-human-sort="stability" data-label="안정" style="min-width:44px;">안정</th>
            <th class="sortable" data-human-sort="tone_fit" data-label="톤" style="min-width:44px;">톤</th>
            <th class="sortable" data-human-sort="avg" data-label="평균" style="min-width:50px;">평균</th>
            <th class="sortable" data-human-sort="ai_avg" data-label="AI 평균" style="min-width:56px;">AI 평균</th>
            <th class="sortable" data-human-sort="ai_pitch" data-label="AI 피치" style="min-width:56px;">AI 피치</th>
            <th class="sortable" data-human-sort="ai_artifact" data-label="AI 튐" style="min-width:56px;">AI 튐</th>
            <th class="sortable" data-human-sort="ai_intonation" data-label="AI 억양" style="min-width:56px;">AI 억양</th>
            <th class="sortable" data-human-sort="ai_fail" data-label="과락" style="min-width:56px;">과락</th>
            <th style="min-width:160px;">AI note</th>
            <th class="sortable" data-human-sort="selected" data-label="선택" style="min-width:56px;">선택</th>
            <th>메모</th>
            <th class="sortable" data-human-sort="status" data-label="status" style="min-width:72px;">status</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
  <div class="modal" id="scriptModal">
    <div class="modal-card">
      <div class="row" style="justify-content:space-between;align-items:center;">
        <div class="modal-title" id="scriptModalTitle">대본</div>
        <button id="scriptModalClose">닫기</button>
      </div>
      <div class="modal-pre" id="scriptModalBody"></div>
    </div>
  </div>
  <script>
    const RUN_ID = {json.dumps(run_id, ensure_ascii=False)};
    const STORAGE_KEY = "seed-lab-eval:" + RUN_ID;
    const AI_STORAGE_KEY = "seed-lab-ai-eval:" + RUN_ID;
    const API_BASE = new URL("./api/", window.location.href);
    const MANIFEST = {manifest_json};
    const SCORE_KEYS = ["naturalness", "pronunciation", "stability", "tone_fit"];
    const AI_SCORE_KEYS = ["naturalness", "pronunciation", "stability", "tone_fit", "pitch_consistency", "artifact_cleanliness", "intonation_similarity"];
    let records = [...MANIFEST];
    let serverConfig = {{
      openai_configured: false,
      auto_eval_on_add: true,
      eval_mode: "",
    }};
    let serverConfigLoaded = false;
    let humanSort = {{ key: "", dir: "asc" }};
    let lastRunStatus = null;

    function emptyEval() {{
      return {{
        naturalness: "",
        pronunciation: "",
        stability: "",
        tone_fit: "",
        pitch_consistency: "",
        artifact_cleanliness: "",
        intonation_similarity: "",
        weighted_ai_score: "",
        weighted_ai_score_raw: "",
        hard_artifact_fail: false,
        hard_artifact_reason: "",
        prosody_fail: false,
        prosody_fail_reason: "",
        rank_excluded: false,
        note: "",
        selected: false,
        updated_at: "",
      }};
    }}

    function nowIso() {{
      return new Date().toISOString();
    }}

    function loadLocalState() {{
      try {{
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return {{}};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {{}};
      }} catch (_e) {{
        return {{}};
      }}
    }}

    function saveLocalState(state) {{
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }}

    function loadAiState() {{
      try {{
        const raw = localStorage.getItem(AI_STORAGE_KEY);
        if (!raw) return {{}};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {{}};
      }} catch (_e) {{
        return {{}};
      }}
    }}

    function saveAiState(state) {{
      localStorage.setItem(AI_STORAGE_KEY, JSON.stringify(state));
    }}

    let humanStateSaveTimer = null;

    function saveState(state) {{
      saveLocalState(state);
      if (humanStateSaveTimer) {{
        clearTimeout(humanStateSaveTimer);
      }}
      humanStateSaveTimer = setTimeout(() => {{
        apiPost("human-evals", {{
          run_id: RUN_ID,
          evaluations: state,
        }}, "PUT").catch((_e) => {{
          // 네트워크 실패 시 localStorage fallback 유지
        }});
      }}, 250);
    }}

    function mergeRecords(base, extras) {{
      const orderedIds = [];
      const map = new Map();
      for (const rec of [...base, ...extras]) {{
        if (!rec || typeof rec !== "object") continue;
        const sampleId = String(rec.sample_id || "").trim();
        if (!sampleId) continue;
        if (!map.has(sampleId)) orderedIds.push(sampleId);
        const prev = map.get(sampleId) || {{}};
        map.set(sampleId, {{ ...prev, ...rec }});
      }}
      return orderedIds.map((id) => map.get(id));
    }}

    function apiUrl(path) {{
      const normalized = String(path || "").replace(/^\\/+/, "");
      return new URL(normalized, API_BASE).toString();
    }}

    async function apiGet(path) {{
      const resp = await fetch(apiUrl(path), {{ method: "GET" }});
      if (!resp.ok) {{
        const text = await resp.text();
        throw new Error(`HTTP ${{resp.status}}: ${{text.slice(0, 200)}}`);
      }}
      return resp.json();
    }}

    async function apiPost(path, body, method = "POST") {{
      const resp = await fetch(apiUrl(path), {{
        method,
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body),
      }});
      const text = await resp.text();
      let parsed = {{}};
      try {{
        parsed = text ? JSON.parse(text) : {{}};
      }} catch (_e) {{
        parsed = {{ message: text }};
      }}
      if (!resp.ok) {{
        const msg = parsed && typeof parsed === "object" ? (parsed.error || parsed.message || text) : text;
        throw new Error(`HTTP ${{resp.status}}: ${{String(msg).slice(0, 300)}}`);
      }}
      return parsed;
    }}

    function scoreAvg(evalObj, aiMode = false) {{
      if (aiMode) {{
        const weighted = Number(evalObj && evalObj.weighted_ai_score);
        if (Number.isFinite(weighted) && weighted > 0) return weighted.toFixed(2);
        const weightedRaw = Number(evalObj && evalObj.weighted_ai_score_raw);
        if (Number.isFinite(weightedRaw) && weightedRaw >= 0) return (1 + weightedRaw * 4).toFixed(2);
      }}
      const keys = aiMode ? AI_SCORE_KEYS : SCORE_KEYS;
      const vals = keys.map(k => Number(evalObj[k])).filter(v => Number.isFinite(v) && v > 0);
      if (!vals.length) return "";
      return (vals.reduce((a,b) => a+b, 0) / vals.length).toFixed(2);
    }}

    function sortArrow(dir) {{
      return dir === "asc" ? " ▲" : " ▼";
    }}

    function updateSortIndicators() {{
      for (const th of document.querySelectorAll("[data-human-sort]")) {{
        const label = String(th.dataset.label || th.textContent || "").replace(/[▲▼]/g, "").trim();
        const key = String(th.dataset.humanSort || "");
        th.textContent = label + (humanSort.key === key ? sortArrow(humanSort.dir) : "");
      }}
    }}

    function comparePrimitive(a, b) {{
      if (typeof a === "number" && typeof b === "number") {{
        return a - b;
      }}
      return String(a).localeCompare(String(b), "ko");
    }}

    function sortHumanRows(items) {{
      if (!humanSort.key) {{
        return items.sort((x, y) => x.index - y.index);
      }}
      const key = humanSort.key;
      const dir = humanSort.dir === "desc" ? -1 : 1;
      const valueOf = (item) => {{
        const rec = item.rec;
        const ev = item.ev;
        const ai = aiState[String(rec.sample_id || "")] || {{}};
        if (key === "seed") return Number(rec.seed || 0);
        if (key === "script_id") return String(rec.script_id || "");
        if (key === "avg") {{
          const v = Number(scoreAvg(ev));
          return Number.isFinite(v) ? v : -1;
        }}
        if (key === "ai_avg") {{
          const raw = Number(ai.weighted_ai_score_raw);
          if (Number.isFinite(raw) && raw >= 0) return raw;
          const v = Number(scoreAvg(ai, true));
          return Number.isFinite(v) ? v : -1;
        }}
        if (key === "ai_pitch") {{
          const v = Number(ai.pitch_consistency);
          return Number.isFinite(v) ? v : -1;
        }}
        if (key === "ai_artifact") {{
          const v = Number(ai.artifact_cleanliness);
          return Number.isFinite(v) ? v : -1;
        }}
        if (key === "ai_intonation") {{
          const v = Number(ai.intonation_similarity);
          return Number.isFinite(v) ? v : -1;
        }}
        if (key === "ai_fail") return ai && (ai.hard_artifact_fail || ai.prosody_fail) ? 1 : 0;
        if (key === "selected") return ev.selected ? 1 : 0;
        if (key === "status") return String(rec.status || "");
        if (SCORE_KEYS.includes(key)) {{
          const v = Number(ev[key]);
          return Number.isFinite(v) ? v : -1;
        }}
        return "";
      }};
      return items.sort((x, y) => {{
        const cmp = comparePrimitive(valueOf(x), valueOf(y));
        if (cmp !== 0) return cmp * dir;
        return x.index - y.index;
      }});
    }}

    function updateSummary(state) {{
      let total = 0;
      let visible = 0;
      let selectedCount = 0;
      let readyCount = 0;
      let scoredCount = 0;
      for (const rec of records) {{
        total += 1;
        if (rec.status === "ready" || rec.status === "skipped_existing") readyCount += 1;
        const sampleId = rec.sample_id;
        if (!state[sampleId]) state[sampleId] = emptyEval();
        const ev = state[sampleId];
        const avg = scoreAvg(ev);
        if (avg !== "") scoredCount += 1;
        if (ev.selected) selectedCount += 1;
        if (rowVisible(rec, ev)) visible += 1;
      }}
      document.getElementById("summary").textContent =
        `rows: ${{total}} / visible: ${{visible}} / ready: ${{readyCount}} / scored: ${{scoredCount}} / selected: ${{selectedCount}}`;
    }}

    function buildScoreSelect(sampleId, key, value, state, onChanged) {{
      const sel = document.createElement("select");
      sel.dataset.sampleId = sampleId;
      sel.dataset.scoreKey = key;
      const opt0 = document.createElement("option");
      opt0.value = "";
      opt0.textContent = "-";
      sel.appendChild(opt0);
      for (let i=1; i<=5; i++) {{
        const opt = document.createElement("option");
        opt.value = String(i);
        opt.textContent = String(i);
        if (String(value) === String(i)) opt.selected = true;
        sel.appendChild(opt);
      }}
      sel.addEventListener("change", () => {{
        if (!state[sampleId]) state[sampleId] = emptyEval();
        state[sampleId][key] = sel.value;
        state[sampleId].updated_at = nowIso();
        saveState(state);
        if (typeof onChanged === "function") onChanged();
      }});
      return sel;
    }}

    function buildMemo(sampleId, value, state) {{
      const ta = document.createElement("textarea");
      ta.className = "memo";
      ta.value = value || "";
      ta.addEventListener("change", () => {{
        if (!state[sampleId]) state[sampleId] = emptyEval();
        state[sampleId].note = ta.value || "";
        state[sampleId].updated_at = nowIso();
        saveState(state);
      }});
      return ta;
    }}

    function buildSelected(sampleId, checked, state, onChanged) {{
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!checked;
      input.addEventListener("change", () => {{
        if (!state[sampleId]) state[sampleId] = emptyEval();
        state[sampleId].selected = input.checked;
        state[sampleId].updated_at = nowIso();
        saveState(state);
        if (typeof onChanged === "function") onChanged();
      }});
      return input;
    }}

    function rowVisible(rec, evalObj) {{
      const seedFilter = document.getElementById("seedFilter").value.trim();
      const scriptFilter = document.getElementById("scriptFilter").value.trim().toLowerCase();
      const statusFilter = document.getElementById("statusFilter").value.trim();
      const selectedOnly = document.getElementById("selectedOnly").checked;
      if (seedFilter && !String(rec.seed).includes(seedFilter)) return false;
      if (scriptFilter && !String(rec.script_id || "").toLowerCase().includes(scriptFilter)) return false;
      if (statusFilter && String(rec.status || "") !== statusFilter) return false;
      if (selectedOnly && !evalObj.selected) return false;
      return true;
    }}

    function render(state) {{
      const tbody = document.getElementById("rows");
      tbody.innerHTML = "";
      const visibleRows = [];
      for (let i = 0; i < records.length; i++) {{
        const rec = records[i];
        const sampleId = rec.sample_id;
        if (!state[sampleId]) state[sampleId] = emptyEval();
        const ev = state[sampleId];
        if (!rowVisible(rec, ev)) continue;
        visibleRows.push({{ rec, ev, index: i }});
      }}
      const rowsToRender = sortHumanRows(visibleRows);
      for (const rowItem of rowsToRender) {{
        const rec = rowItem.rec;
        const ev = rowItem.ev;
        const sampleId = rec.sample_id;
        const ai = aiState[String(sampleId)] || {{}};
        const tr = document.createElement("tr");

        const tdSeed = document.createElement("td");
        tdSeed.textContent = String(rec.seed);
        tr.appendChild(tdSeed);

        const tdScriptId = document.createElement("td");
        tdScriptId.textContent = String(rec.script_id || "");
        tr.appendChild(tdScriptId);

        const tdScript = document.createElement("td");
        const scriptTitle = document.createElement("div");
        scriptTitle.textContent = String(rec.script_title || "");
        tdScript.appendChild(scriptTitle);
        const scriptBtn = document.createElement("button");
        scriptBtn.className = "script-btn";
        scriptBtn.type = "button";
        scriptBtn.textContent = "기준대본 보기";
        scriptBtn.addEventListener("click", () => {{
          openScriptModal(
            `seed=${{rec.seed}} / ${{rec.script_id || ""}}`,
            String(rec.script_text || "")
          );
        }});
        tdScript.appendChild(scriptBtn);
        tr.appendChild(tdScript);

        const tdAudio = document.createElement("td");
        if ((rec.status === "ready" || rec.status === "skipped_existing") && rec.audio_rel_path) {{
          const audio = document.createElement("audio");
          audio.controls = true;
          audio.preload = "none";
          audio.src = String(rec.audio_url || rec.audio_rel_path);
          tdAudio.appendChild(audio);
        }} else {{
          tdAudio.textContent = rec.error ? String(rec.error).slice(0, 120) : "no audio";
        }}
        tr.appendChild(tdAudio);

        for (const key of SCORE_KEYS) {{
          const td = document.createElement("td");
          td.appendChild(
            buildScoreSelect(sampleId, key, ev[key], state, () => {{
              tdAvg.textContent = scoreAvg(state[sampleId] || emptyEval());
              updateSummary(state);
            }})
          );
          tr.appendChild(td);
        }}

        const tdAvg = document.createElement("td");
        tdAvg.textContent = scoreAvg(ev);
        tr.appendChild(tdAvg);

        const tdAiAvg = document.createElement("td");
        tdAiAvg.textContent = scoreAvg(ai, true) || "-";
        tr.appendChild(tdAiAvg);

        const tdAiPitch = document.createElement("td");
        tdAiPitch.textContent = String(ai.pitch_consistency || "-");
        tr.appendChild(tdAiPitch);

        const tdAiArtifact = document.createElement("td");
        tdAiArtifact.textContent = String(ai.artifact_cleanliness || "-");
        tr.appendChild(tdAiArtifact);

        const tdAiIntonation = document.createElement("td");
        tdAiIntonation.textContent = String(ai.intonation_similarity || "-");
        tr.appendChild(tdAiIntonation);

        const tdAiFail = document.createElement("td");
        const aiFailed = !!(ai && (ai.hard_artifact_fail || ai.prosody_fail));
        tdAiFail.textContent = aiFailed ? "Y" : "-";
        tdAiFail.className = aiFailed ? "bad" : "";
        tr.appendChild(tdAiFail);

        const tdAiNote = document.createElement("td");
        let failPrefix = "";
        if (ai && ai.hard_artifact_fail) {{
          failPrefix += `[과락:${{String(ai.hard_artifact_reason || "artifact")}}] `;
        }}
        if (ai && ai.prosody_fail) {{
          failPrefix += `[억양과락:${{String(ai.prosody_fail_reason || "prosody")}}] `;
        }}
        tdAiNote.textContent = failPrefix + String(ai.note || "-");
        tr.appendChild(tdAiNote);

        const tdSel = document.createElement("td");
        tdSel.appendChild(
          buildSelected(sampleId, ev.selected, state, () => {{
            updateSummary(state);
          }})
        );
        tr.appendChild(tdSel);

        const tdMemo = document.createElement("td");
        tdMemo.appendChild(buildMemo(sampleId, ev.note, state));
        tr.appendChild(tdMemo);

        const tdStatus = document.createElement("td");
        tdStatus.textContent = String(rec.status || "");
        tdStatus.className = (rec.status === "ready" || rec.status === "skipped_existing") ? "ok" : "bad";
        tr.appendChild(tdStatus);

        tbody.appendChild(tr);
      }}
      saveState(state);
      updateSummary(state);
      updateAiSummary(aiState);
      updateAiHint(aiState);
      updateRunStatusMeta(lastRunStatus);
      updateSortIndicators();
    }}

    function updateAiSummary(aiMap) {{
      const total = Object.keys(aiMap).length;
      let ready = 0;
      let failed = 0;
      let runpod = 0;
      let gpu = 0;
      for (const value of Object.values(aiMap)) {{
        if (!value || typeof value !== "object") continue;
        if (String(value.auto_eval_status || "ready") === "ready") ready += 1;
        else failed += 1;
        if (String(value.executor || "") === "runpod_gpu") runpod += 1;
        const caps = value.capabilities && typeof value.capabilities === "object" ? value.capabilities : {{}};
        if (caps.gpu_acceleration_active === true) gpu += 1;
      }}
      document.getElementById("aiSummary").textContent =
        `rows: ${{total}} / ready: ${{ready}} / failed: ${{failed}} / runpod: ${{runpod}} / gpu: ${{gpu}}`;
    }}

    function updateAiHint(aiMap) {{
      const total = Object.keys(aiMap).length;
      const aiHint = document.getElementById("aiHint");
      if (!aiHint) return;
      if (!serverConfigLoaded) {{
        aiHint.textContent = "AI 평가 상태를 확인할 수 없습니다. (serve API 미연결)";
        return;
      }}
      if (total > 0) {{
        aiHint.textContent = "AI 평가 데이터가 로드되었습니다.";
        return;
      }}
      if (!serverConfig.openai_configured) {{
        aiHint.textContent = "OPENAI_API_KEY_SEEDLAB_ASR / OPENAI_API_KEY_SEEDLAB_JUDGE 미설정(또는 quickstart env 미전달): 자동 AI 평가는 비활성 상태입니다.";
        return;
      }}
      if (!serverConfig.auto_eval_on_add) {{
        aiHint.textContent = "자동 AI 평가가 비활성화되어 있습니다. (--disable-auto-eval-on-add)";
        return;
      }}
      aiHint.textContent = "아직 AI 평가 데이터가 없습니다. '평가 테이블에 추가' 체크 후 TTS 생성하거나 auto-eval을 실행하세요.";
    }}

    function updateRunStatusMeta(statusPayload) {{
      const target = document.getElementById("runStatusMeta");
      if (!target) return;
      if (!statusPayload || typeof statusPayload !== "object") {{
        target.textContent = "";
        return;
      }}
      const executorCounts = statusPayload.eval_executor_counts && typeof statusPayload.eval_executor_counts === "object"
        ? statusPayload.eval_executor_counts
        : {{}};
      const avgTimings = statusPayload.avg_stage_timings_ms && typeof statusPayload.avg_stage_timings_ms === "object"
        ? statusPayload.avg_stage_timings_ms
        : {{}};
      const executorParts = Object.entries(executorCounts).map(([key, value]) => `${{String(key)}}=${{Number(value || 0)}}`);
      const timingParts = [];
      for (const key of ["reference_load", "asr", "signal_analysis", "judge_note", "total"]) {{
        const value = Number(avgTimings[key]);
        if (Number.isFinite(value) && value > 0) {{
          timingParts.push(`${{key}}=${{value.toFixed(1)}}ms`);
        }}
      }}
      const chunks = [
        `mode=${{serverConfig.eval_mode || "-"}}`,
        `runpod=${{Number(statusPayload.runpod_job_count || 0)}}`,
        `gpu=${{Number(statusPayload.gpu_active_sample_count || 0)}}`,
        `remote_failed=${{Number(statusPayload.remote_eval_failed_count || 0)}}`,
      ];
      if (executorParts.length > 0) {{
        chunks.push(`executors=[${{executorParts.join(", ")}}]`);
      }}
      if (timingParts.length > 0) {{
        chunks.push(`avg=[${{timingParts.join(", ")}}]`);
      }}
      if (statusPayload.remote_eval_last_error) {{
        chunks.push(`last_remote_error=${{String(statusPayload.remote_eval_last_error).slice(0, 160)}}`);
      }}
      target.textContent = chunks.join(" / ");
    }}

    function toggleSort(key) {{
      if (!key) return;
      if (humanSort.key === key) {{
        humanSort.dir = humanSort.dir === "asc" ? "desc" : "asc";
      }} else {{
        humanSort.key = key;
        humanSort.dir = (key === "avg" || key === "ai_avg") ? "desc" : "asc";
      }}
      render(state);
    }}

    function exportJson(state) {{
      const selectedOnly = {{}};
      for (const [sampleId, value] of Object.entries(state)) {{
        if (!value || typeof value !== "object") continue;
        if (value.selected === true) {{
          selectedOnly[sampleId] = value;
        }}
      }}
      if (Object.keys(selectedOnly).length === 0) {{
        alert("선택된 후보가 없습니다. 먼저 '선택' 체크를 해주세요.");
        return;
      }}
      const payload = {{
        run_id: RUN_ID,
        exported_at: nowIso(),
        evaluations: selectedOnly,
      }};
      const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: "application/json" }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `seed-lab-eval-${{RUN_ID}}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }}

    function importJson(file, state) {{
      const reader = new FileReader();
      reader.onload = () => {{
        try {{
          const parsed = JSON.parse(String(reader.result || "{{}}"));
          const incoming = parsed && typeof parsed === "object" ? (parsed.evaluations || parsed) : {{}};
          if (!incoming || typeof incoming !== "object") throw new Error("invalid format");
          for (const [sampleId, value] of Object.entries(incoming)) {{
            if (!value || typeof value !== "object") continue;
            state[sampleId] = {{
              ...emptyEval(),
              ...state[sampleId],
              ...value,
            }};
          }}
          saveState(state);
          render(state);
          alert("평가 import 완료");
        }} catch (e) {{
          alert("평가 import 실패: " + e);
        }}
      }};
      reader.readAsText(file, "utf-8");
    }}

    function exportAiJson(aiMap) {{
      const payload = {{
        run_id: RUN_ID,
        exported_at: nowIso(),
        mode: "auto_eval_hybrid_v3",
        evaluations: aiMap,
      }};
      const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: "application/json" }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `seed-lab-ai-eval-${{RUN_ID}}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }}

    async function refreshLiveRecords(renderAfter = true) {{
      try {{
        const payload = await apiGet("live-records");
        const incoming = Array.isArray(payload.records) ? payload.records : [];
        records = mergeRecords(MANIFEST, incoming);
        if (renderAfter) render(state);
      }} catch (_e) {{
        records = mergeRecords(MANIFEST, []);
        if (renderAfter) render(state);
      }}
    }}

    async function refreshAiState(renderAfter = true) {{
      try {{
        const payload = await apiGet("ai-evals");
        const incoming = payload && typeof payload === "object" ? payload.evaluations : {{}};
        aiState = incoming && typeof incoming === "object" ? incoming : {{}};
        saveAiState(aiState);
      }} catch (_e) {{
        aiState = loadAiState();
      }}
      if (renderAfter) render(state);
    }}

    async function refreshHumanState(renderAfter = true) {{
      try {{
        const payload = await apiGet("human-evals");
        const incoming = payload && typeof payload === "object" ? payload.evaluations : {{}};
        if (incoming && typeof incoming === "object") {{
          for (const [sampleId, value] of Object.entries(incoming)) {{
            if (!value || typeof value !== "object") continue;
            state[sampleId] = {{
              ...emptyEval(),
              ...state[sampleId],
              ...value,
            }};
          }}
          saveLocalState(state);
        }}
      }} catch (_e) {{
        // localStorage fallback 유지
      }}
      if (renderAfter) render(state);
    }}

    async function refreshRunStatus() {{
      try {{
        lastRunStatus = await apiGet("run-status");
        render(state);
        return lastRunStatus;
      }} catch (_e) {{
        lastRunStatus = null;
        return null;
      }}
    }}

    async function loadServerConfig() {{
      try {{
        const payload = await apiGet("config");
        serverConfigLoaded = true;
        serverConfig = {{
          openai_configured: !!(payload && payload.openai_configured),
          auto_eval_on_add: payload && payload.auto_eval_on_add !== false,
          eval_mode: String((payload && payload.eval_mode) || ""),
        }};
        const p = payload && typeof payload === "object" ? (payload.default_tts_params || {{}}) : {{}};
        if (p.text_lang) pgTextLang.value = String(p.text_lang);
        if (p.prompt_lang) pgPromptLang.value = String(p.prompt_lang);
        if (p.top_k) pgTopK.value = String(p.top_k);
        if (p.sample_steps) pgSampleSteps.value = String(p.sample_steps);
        if (p.fragment_interval !== undefined && p.fragment_interval !== null) {{
          pgFragmentInterval.value = String(p.fragment_interval);
        }}
        if (p.super_sampling !== undefined && p.super_sampling !== null) {{
          pgSuperSampling.checked = !!p.super_sampling;
        }}
        if (p.ref_audio_path) pgRefAudioPath.value = String(p.ref_audio_path);
        if (p.prompt_text) pgPromptText.value = String(p.prompt_text);
      }} catch (_e) {{
        serverConfigLoaded = false;
        serverConfig = {{
          openai_configured: false,
          auto_eval_on_add: true,
          eval_mode: "",
        }};
        // file:// 또는 API 미기동 상태에서는 무시
      }}
    }}

    const state = loadLocalState();
    let aiState = loadAiState();
    const scriptModal = document.getElementById("scriptModal");
    const scriptModalTitle = document.getElementById("scriptModalTitle");
    const scriptModalBody = document.getElementById("scriptModalBody");
    const scriptModalClose = document.getElementById("scriptModalClose");
    const pgSeed = document.getElementById("pgSeed");
    const pgTextLang = document.getElementById("pgTextLang");
    const pgPromptLang = document.getElementById("pgPromptLang");
    const pgTopK = document.getElementById("pgTopK");
    const pgSampleSteps = document.getElementById("pgSampleSteps");
    const pgFragmentInterval = document.getElementById("pgFragmentInterval");
    const pgSuperSampling = document.getElementById("pgSuperSampling");
    const pgAddToReview = document.getElementById("pgAddToReview");
    const pgRefAudioPath = document.getElementById("pgRefAudioPath");
    const pgPromptText = document.getElementById("pgPromptText");
    const pgScriptText = document.getElementById("pgScriptText");
    const pgGenerateBtn = document.getElementById("pgGenerateBtn");
    const pgStatus = document.getElementById("pgStatus");
    const pgAudio = document.getElementById("pgAudio");

    function openScriptModal(title, text) {{
      scriptModalTitle.textContent = title || "대본";
      scriptModalBody.textContent = text || "(대본 없음)";
      scriptModal.style.display = "flex";
    }}

    function closeScriptModal() {{
      scriptModal.style.display = "none";
    }}

    scriptModalClose.addEventListener("click", closeScriptModal);
    scriptModal.addEventListener("click", (e) => {{
      if (e.target === scriptModal) closeScriptModal();
    }});
    document.addEventListener("keydown", (e) => {{
      if (e.key === "Escape" && scriptModal.style.display === "flex") {{
        closeScriptModal();
      }}
    }});

    document.getElementById("seedFilter").addEventListener("input", () => render(state));
    document.getElementById("scriptFilter").addEventListener("input", () => render(state));
    document.getElementById("statusFilter").addEventListener("change", () => render(state));
    document.getElementById("selectedOnly").addEventListener("change", () => render(state));
    for (const th of document.querySelectorAll("[data-human-sort]")) {{
      th.addEventListener("click", () => toggleSort(String(th.dataset.humanSort || "")));
    }}
    document.getElementById("resetBtn").addEventListener("click", () => {{
      document.getElementById("seedFilter").value = "";
      document.getElementById("scriptFilter").value = "";
      document.getElementById("statusFilter").value = "";
      document.getElementById("selectedOnly").checked = false;
      render(state);
    }});
    document.getElementById("exportBtn").addEventListener("click", () => exportJson(state));
    document.getElementById("importBtn").addEventListener("click", () => {{
      const input = document.getElementById("importFile");
      if (!input.files || !input.files.length) {{
        alert("import 파일을 선택하세요.");
        return;
      }}
      importJson(input.files[0], state);
    }});
    document.getElementById("aiExportBtn").addEventListener("click", () => exportAiJson(aiState));

    pgGenerateBtn.addEventListener("click", async () => {{
      const scriptText = String(pgScriptText.value || "").trim();
      if (!scriptText) {{
        alert("샘플 텍스트를 입력하세요.");
        return;
      }}
      const payload = {{
        script_text: scriptText,
        seed: String(pgSeed.value || "").trim(),
        text_lang: String(pgTextLang.value || "").trim(),
        prompt_lang: String(pgPromptLang.value || "").trim(),
        top_k: String(pgTopK.value || "").trim(),
        sample_steps: String(pgSampleSteps.value || "").trim(),
        fragment_interval: String(pgFragmentInterval.value || "").trim(),
        super_sampling: !!pgSuperSampling.checked,
        add_to_review: !!pgAddToReview.checked,
        ref_audio_path: String(pgRefAudioPath.value || "").trim(),
        prompt_text: String(pgPromptText.value || "").trim(),
      }};

      pgGenerateBtn.disabled = true;
      pgStatus.textContent = "생성 중...";
      try {{
        const out = await apiPost("tts/generate", payload);
        const audioUrl = String(out.audio_url || "");
        if (audioUrl) {{
          pgAudio.src = audioUrl + `?t=${{Date.now()}}`;
          pgAudio.load();
        }}
        if (out.record && typeof out.record === "object" && payload.add_to_review) {{
          records = mergeRecords(records, [out.record]);
        }}
        if (out.ai_eval && out.record && out.record.sample_id) {{
          aiState[String(out.record.sample_id)] = out.ai_eval;
          saveAiState(aiState);
        }}
        if (out.seed) {{
          pgSeed.value = String(out.seed);
        }}
        render(state);
        const aiMsg = out.ai_eval ? " / AI평가 완료" : "";
        pgStatus.textContent = `완료 (seed=${{out.seed || "-"}})${{aiMsg}}`;
      }} catch (e) {{
        pgStatus.textContent = `실패: ${{String(e)}}`;
      }} finally {{
        pgGenerateBtn.disabled = false;
      }}
    }});

    if (!pgScriptText.value && MANIFEST.length > 0) {{
      pgScriptText.value = String(MANIFEST[0].script_text || "");
    }}
    render(state);
    loadServerConfig()
      .then(() => refreshRunStatus())
      .then(() => refreshHumanState(false))
      .then(() => refreshLiveRecords(false))
      .then(() => refreshAiState(true));
    const pollHandle = setInterval(() => {{
      refreshRunStatus().then((statusPayload) => {{
        const runStatus = String((statusPayload && statusPayload.status) || "");
        refreshLiveRecords(false).then(() => refreshAiState(true));
        if (runStatus === "ready" || runStatus === "failed") {{
          clearInterval(pollHandle);
        }}
      }});
    }}, 5000);
  </script>
</body>
</html>
"""
    html_path.write_text(html_body, encoding="utf-8")


def _write_manifest(run_dir: Path, meta: dict[str, Any], records: list[dict[str, Any]]) -> None:
    manifest = {
        "meta": meta,
        "records": records,
        "generated_at": dt.datetime.now().isoformat(),
    }
    manifest_json = run_dir / "manifest.json"
    manifest_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest_jsonl = run_dir / "manifest.jsonl"
    with manifest_jsonl.open("w", encoding="utf-8") as fp:
        for rec in records:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _worker_generate_one(
    endpoint: str,
    timeout_seconds: int,
    tts_params: dict[str, Any],
    run_dir: Path,
    seed: int,
    script: ScriptItem,
    take_index: int,
    retries: int,
) -> dict[str, Any]:
    sample_id = f"{script.script_id}:{seed}:t{take_index}"
    audio_dir = run_dir / "audio" / script.script_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_filename = f"seed-{seed}-t{take_index}.wav"
    audio_path = audio_dir / audio_filename
    audio_rel = os.path.relpath(audio_path, run_dir).replace("\\", "/")

    attempt = 0
    last_error = ""
    network_backoff_seconds = 1.5
    while attempt <= retries:
        attempt += 1
        try:
            payload = _build_payload(script.text, tts_params, seed)
            audio_bytes = _http_post_tts(endpoint, payload, timeout_seconds=timeout_seconds)
            if not audio_bytes:
                raise RuntimeError("empty response body")
            audio_path.write_bytes(audio_bytes)
            return {
                "sample_id": sample_id,
                "seed": seed,
                "take_index": take_index,
                "script_id": script.script_id,
                "script_title": script.title,
                "script_text": script.text,
                "audio_rel_path": audio_rel,
                "status": "ready",
                "error_type": "",
                "error": "",
                "bytes": len(audio_bytes),
                "tts_params": {
                    "ref_audio_path": str(tts_params.get("ref_audio_path") or ""),
                    "reference_audio_local_path": str(tts_params.get("reference_audio_local_path") or ""),
                },
            }
        except Exception as e:
            last_error = str(e)
            should_backoff = _is_network_origin_error(last_error)
            if should_backoff and attempt <= retries:
                wait_seconds = min(12.0, network_backoff_seconds * (2 ** (attempt - 1)))
                time.sleep(wait_seconds)
            if attempt > retries:
                break
    error_type = "network_origin_unreachable" if _is_network_origin_error(last_error) else "generation_error"
    return {
        "sample_id": sample_id,
        "seed": seed,
        "take_index": take_index,
        "script_id": script.script_id,
        "script_title": script.title,
        "script_text": script.text,
        "audio_rel_path": "",
        "status": "failed",
        "error_type": error_type,
        "error": last_error[:800],
        "bytes": 0,
        "tts_params": {
            "ref_audio_path": str(tts_params.get("ref_audio_path") or ""),
            "reference_audio_local_path": str(tts_params.get("reference_audio_local_path") or ""),
        },
    }


def cmd_run(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset).resolve()
    scripts, tts_params = load_dataset(dataset_path)
    endpoint = _resolve_api_endpoint(args.api_url or os.getenv("TTS_API_URL", ""))

    stage = args.stage
    selected_scripts = _pick_scripts_for_stage(scripts, stage=stage, script_ids=_parse_script_ids(args.script_ids))

    requested_samples = int(args.samples)
    if requested_samples <= 0:
        raise RuntimeError("samples must be > 0")
    takes_per_seed = int(args.takes_per_seed)
    if takes_per_seed <= 0:
        raise RuntimeError("takes_per_seed must be > 0")

    seed_list: list[int]
    seed_mode = "random_only"
    random_fill_count = 0
    truncated_count = 0
    if args.seed_list:
        seed_mode = "explicit_plus_random_fill"
        seed_list, random_fill_count, truncated_count = _expand_seed_list_with_random(
            _parse_seed_values(args.seed_list),
            requested_samples,
        )
    elif args.seeds_file:
        seed_mode = "explicit_plus_random_fill"
        seed_list, random_fill_count, truncated_count = _expand_seed_list_with_random(
            _load_seeds_file(Path(args.seeds_file).resolve()),
            requested_samples,
        )
    else:
        if stage == "b":
            raise RuntimeError("stage b requires --seed-list or --seeds-file")
        seed_list = _random_unique_seeds(requested_samples)

    if not seed_list:
        raise RuntimeError("no seeds selected")

    run_id = args.run_id or _build_run_id(stage)
    output_root = Path(args.output_root or DEFAULT_OUTPUT_ROOT).resolve()
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    tasks: list[tuple[int, ScriptItem, int]] = []
    for seed in seed_list:
        for script in selected_scripts:
            for take_index in range(1, takes_per_seed + 1):
                tasks.append((seed, script, take_index))

    started_at = dt.datetime.now().isoformat()
    records: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as pool:
        futures: list[concurrent.futures.Future[dict[str, Any]]] = []
        for seed, script, take_index in tasks:
            futures.append(
                pool.submit(
                    _worker_generate_one,
                    endpoint,
                    int(args.timeout),
                    tts_params,
                    run_dir,
                    seed,
                    script,
                    take_index,
                    int(args.retries),
                )
            )
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            records.append(rec)
            done_count += 1
            if done_count % 10 == 0 or done_count == len(futures):
                print(f"[seed-lab] progress {done_count}/{len(futures)}", flush=True)

    records.sort(
        key=lambda r: (
            str(r.get("script_id", "")),
            int(r.get("seed", 0)),
            int(r.get("take_index", 0)),
        )
    )
    ready = sum(1 for r in records if r.get("status") in ("ready", "skipped_existing"))
    failed = sum(1 for r in records if r.get("status") == "failed")
    network_fail_count = sum(1 for r in records if r.get("status") == "failed" and r.get("error_type") == "network_origin_unreachable")
    http_502_count = sum(1 for r in records if r.get("status") == "failed" and "http 502" in str(r.get("error", "")).lower())

    meta = {
        "run_id": run_id,
        "stage": stage,
        "dataset": str(dataset_path),
        "api_endpoint": endpoint,
        "started_at": started_at,
        "finished_at": dt.datetime.now().isoformat(),
        "seed_count": len(seed_list),
        "seed_list": seed_list,
        "seed_mode": seed_mode,
        "random_fill_count": random_fill_count,
        "truncated_count": truncated_count,
        "script_ids": [s.script_id for s in selected_scripts],
        "script_titles": [s.title for s in selected_scripts],
        "takes_per_seed": takes_per_seed,
        "concurrency": int(args.concurrency),
        "timeout_seconds": int(args.timeout),
        "retries": int(args.retries),
        "ready_count": ready,
        "failed_count": failed,
        "network_fail_count": network_fail_count,
        "http_502_count": http_502_count,
    }
    _write_manifest(run_dir, meta, records)
    _generate_review_html(run_id, records, run_dir / "index.html")

    print("")
    print(f"[seed-lab] run_id={run_id}")
    print(f"[seed-lab] output={run_dir}")
    print(f"[seed-lab] takes_per_seed={takes_per_seed}")
    print(f"[seed-lab] ready={ready} failed={failed} total={len(records)}")
    print(f"[seed-lab] network_fail={network_fail_count} http_502={http_502_count}")
    print(f"[seed-lab] review_html={run_dir / 'index.html'}")
    print("")
    print("[next]")
    print(f"1) 브라우저에서 index.html 열고 점수/메모 입력 후 Export JSON")
    print(
        "2) report 생성: "
        f"python3 scripts/seed_lab.py report --run-dir {sh_escape(str(run_dir))} --eval-json <exported_eval.json> --top 20 --prepare-stage-b"
    )
    if stage == "a":
        print(
            "3) stage-b 실행: "
            f"python3 scripts/seed_lab.py run --dataset {sh_escape(str(dataset_path))} --api-url {sh_escape(endpoint)} --stage b "
            f"--seeds-file {sh_escape(str(run_dir / 'top_seeds_stage_b.txt'))}"
        )
    return 0


def _resolve_openai_keys(
    *,
    explicit_shared_key: str,
    explicit_asr_key: str,
    explicit_judge_key: str,
) -> tuple[str, str]:
    fallback_key = (
        (explicit_shared_key or "").strip()
        or (os.getenv("OPENAI_FALLBACK_API_KEY") or "").strip()
        or (os.getenv("OPENAI_API_KEY") or "").strip()
    )
    asr_key = (
        (explicit_asr_key or "").strip()
        or (os.getenv("OPENAI_API_KEY_SEEDLAB_ASR") or "").strip()
        or fallback_key
    )
    judge_key = (
        (explicit_judge_key or "").strip()
        or (os.getenv("OPENAI_API_KEY_SEEDLAB_JUDGE") or "").strip()
        or fallback_key
    )
    if not asr_key or not judge_key:
        raise RuntimeError(
            "OpenAI key missing: set OPENAI_API_KEY_SEEDLAB_ASR and OPENAI_API_KEY_SEEDLAB_JUDGE "
            "(or OPENAI_FALLBACK_API_KEY / OPENAI_API_KEY)."
        )
    return asr_key, judge_key


def _resolve_asr_model_for_transcription(requested: str) -> tuple[str, str]:
    raw = (requested or "").strip()
    if not raw:
        return DEFAULT_AUTO_EVAL_ASR_MODEL, "empty asr model -> fallback"
    lowered = raw.lower()
    if lowered == "whisper-1" or "transcribe" in lowered:
        return raw, ""
    return DEFAULT_AUTO_EVAL_ASR_MODEL, f"asr model '{raw}' is not a transcription model; fallback to '{DEFAULT_AUTO_EVAL_ASR_MODEL}'"


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged_headers = {"Content-Type": "application/json"}
    merged_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=merged_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise RuntimeError(f"non-object response from {url}")
            return parsed
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {body[:1200]}") from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _http_post_multipart(
    url: str,
    *,
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    boundary = f"----seedlab-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    filename = file_path.name
    mime = mimetypes.guess_type(filename)[0] or "audio/wav"
    file_bytes = file_path.read_bytes()
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)

    merged_headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    merged_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise RuntimeError(f"non-object response from {url}")
            return parsed
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {body_text[:1200]}") from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = (text or "").strip()
    if not candidate:
        return {}
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_compare_text(text: str) -> str:
    lowered = (text or "").lower()
    collapsed = re.sub(r"\s+", "", lowered)
    return re.sub(r"[^0-9a-z가-힣]", "", collapsed)


def _levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, ch_left in enumerate(left, start=1):
        curr = [i]
        for j, ch_right in enumerate(right, start=1):
            insert_cost = curr[j - 1] + 1
            delete_cost = prev[j] + 1
            replace_cost = prev[j - 1] + (0 if ch_left == ch_right else 1)
            curr.append(min(insert_cost, delete_cost, replace_cost))
        prev = curr
    return prev[-1]


def _char_accuracy(reference: str, hypothesis: str) -> float:
    if not reference and not hypothesis:
        return 1.0
    if not reference:
        return 0.0
    dist = _levenshtein_distance(reference, hypothesis)
    base = max(1, len(reference))
    return max(0.0, 1.0 - (dist / base))


def _wav_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return float(frames) / float(rate)
    except Exception:
        return 0.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _mean(values: list[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = _clamp(q, 0.0, 1.0) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def _safe_div(num: float, den: float) -> float:
    return float(num / den) if den else 0.0


def _sigmoid01(value: float, *, center: float, width: float) -> float:
    width = max(1e-6, float(width))
    return 1.0 / (1.0 + math.exp(-((float(value) - center) / width)))


def _linear_similarity(value: float, *, ideal: float, tolerance: float) -> float:
    tolerance = max(1e-6, float(tolerance))
    return _clamp(1.0 - (abs(float(value) - ideal) / tolerance), 0.0, 1.0)


def _pairwise_mean(values: list[float]) -> float | None:
    cleaned = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not cleaned:
        return None
    return _mean(cleaned)


def _map_similarity_to_score(value: float | None, *, floor: float = 1.0, ceil: float = 5.0) -> float | None:
    if value is None:
        return None
    return round(_clamp(floor + (ceil - floor) * float(value), floor, ceil), 2)


def _read_wav_mono_samples(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        sample_rate = int(wf.getframerate() or 0)
        sample_width = int(wf.getsampwidth() or 0)
        channels = int(wf.getnchannels() or 1)
    if sample_rate <= 0 or sample_width <= 0:
        return [], 0
    mono = frames
    if channels > 1:
        mono = audioop.tomono(frames, sample_width, 0.5, 0.5)
    if sample_width == 1:
        raw = array("B", mono)
        samples = [(float(v) - 128.0) / 128.0 for v in raw]
    elif sample_width == 2:
        raw = array("h")
        raw.frombytes(mono)
        if sys.byteorder != "little":
            raw.byteswap()
        samples = [float(v) / 32768.0 for v in raw]
    elif sample_width == 3:
        widened = audioop.lin2lin(mono, 3, 4)
        raw = array("i")
        raw.frombytes(widened)
        if sys.byteorder != "little":
            raw.byteswap()
        samples = [float(v) / 2147483648.0 for v in raw]
    else:
        raw = array("i")
        raw.frombytes(audioop.lin2lin(mono, sample_width, 4))
        if sys.byteorder != "little":
            raw.byteswap()
        samples = [float(v) / 2147483648.0 for v in raw]
    return samples, sample_rate


def _estimate_pitch_hz(frame: list[float], sample_rate: int) -> float:
    if not frame or sample_rate <= 0:
        return 0.0
    target_rate = 8000
    stride = max(1, int(sample_rate / target_rate))
    if stride > 1:
        frame = frame[::stride]
        sample_rate = max(1, int(sample_rate / stride))
    energy = math.sqrt(sum(v * v for v in frame) / len(frame))
    if energy < 0.015:
        return 0.0
    min_f0 = 70.0
    max_f0 = 400.0
    min_lag = max(1, int(sample_rate / max_f0))
    max_lag = min(len(frame) - 2, int(sample_rate / min_f0))
    if max_lag <= min_lag:
        return 0.0
    denom = sum(v * v for v in frame)
    if denom <= 1e-9:
        return 0.0
    best_lag = 0
    best_corr = 0.0
    for lag in range(min_lag, max_lag + 1):
        corr = 0.0
        upto = len(frame) - lag
        for i in range(upto):
            corr += frame[i] * frame[i + lag]
        normalized = corr / denom
        if normalized > best_corr:
            best_corr = normalized
            best_lag = lag
    if best_lag <= 0 or best_corr < 0.30:
        return 0.0
    return float(sample_rate) / float(best_lag)


def _prosody_features_from_samples(samples: list[float], sample_rate: int) -> dict[str, Any]:
    if not samples or sample_rate <= 0:
        return {
            "duration_sec": 0.0,
            "f0_median_hz": 0.0,
            "f0_iqr_hz": 0.0,
            "voiced_ratio": 0.0,
            "pitch_jump_rate": 1.0,
            "pitch_dropout_rate": 1.0,
            "rms_jump_rate": 1.0,
            "spectral_flux_spike_rate": 1.0,
            "zcr_spike_rate": 1.0,
            "clipping_ratio": 1.0,
            "short_pause_break_rate": 1.0,
            "energy_cv": 1.0,
            "pitch_cv": 1.0,
            "pause_density": 1.0,
            "voiced_segment_rate": 1.0,
            "worst_artifact_window_sec": 0.0,
        }

    frame_size = max(256, int(sample_rate * 0.02))
    hop_size = max(128, int(sample_rate * 0.01))
    rms_values: list[float] = []
    zcr_values: list[float] = []
    flux_values: list[float] = []
    pitch_values: list[float] = []
    pause_count = 0
    silent_run = 0
    prev_energy_band: list[float] | None = None
    voiced_segments = 0
    in_voiced = False

    for start in range(0, max(1, len(samples) - frame_size + 1), hop_size):
        frame = samples[start : start + frame_size]
        if len(frame) < frame_size:
            break
        rms = math.sqrt(sum(v * v for v in frame) / len(frame))
        rms_values.append(rms)
        zero_crossings = 0
        for left, right in zip(frame[:-1], frame[1:]):
            if (left <= 0 < right) or (left >= 0 > right):
                zero_crossings += 1
        zcr_values.append(float(zero_crossings) / max(1, len(frame) - 1))

        band_size = max(1, len(frame) // 8)
        current_energy_band: list[float] = []
        for band_idx in range(8):
            band = frame[band_idx * band_size : (band_idx + 1) * band_size]
            if not band:
                continue
            current_energy_band.append(sum(abs(v) for v in band) / len(band))
        if prev_energy_band and current_energy_band and len(prev_energy_band) == len(current_energy_band):
            flux = 0.0
            for prev_val, curr_val in zip(prev_energy_band, current_energy_band):
                flux += abs(curr_val - prev_val)
            flux_values.append(flux / len(current_energy_band))
        prev_energy_band = current_energy_band

        if rms < 0.008:
            silent_run += 1
        else:
            if silent_run >= 3:
                pause_count += 1
            silent_run = 0

        pitch_hz = _estimate_pitch_hz(frame, sample_rate)
        if pitch_hz > 0:
            pitch_values.append(pitch_hz)
            if not in_voiced:
                voiced_segments += 1
                in_voiced = True
        else:
            in_voiced = False

    duration_sec = len(samples) / float(sample_rate)
    voiced_ratio = _safe_div(len(pitch_values), len(rms_values))
    f0_median_hz = _median(pitch_values)
    f0_iqr_hz = max(0.0, _quantile(pitch_values, 0.75) - _quantile(pitch_values, 0.25))
    clipping_ratio = _safe_div(sum(1 for s in samples if abs(s) >= 0.985), len(samples))

    pitch_jumps = 0
    for left, right in zip(pitch_values[:-1], pitch_values[1:]):
        ratio = max(left, right) / max(1e-6, min(left, right))
        if ratio > 1.22:
            pitch_jumps += 1
    pitch_jump_rate = _safe_div(pitch_jumps, max(1, len(pitch_values) - 1))
    pitch_dropout_rate = 1.0 - voiced_ratio

    rms_jump_count = 0
    worst_rms_jump = 0.0
    worst_rms_idx = 0
    for idx, (left, right) in enumerate(zip(rms_values[:-1], rms_values[1:])):
        if left < 1e-6:
            continue
        jump = abs(math.log((right + 1e-6) / (left + 1e-6)))
        if jump > 0.70:
            rms_jump_count += 1
        if jump > worst_rms_jump:
            worst_rms_jump = jump
            worst_rms_idx = idx
    rms_jump_rate = _safe_div(rms_jump_count, max(1, len(rms_values) - 1))

    flux_baseline = _quantile(flux_values, 0.8) if flux_values else 0.0
    flux_spike_count = sum(1 for val in flux_values if flux_baseline > 0 and val > flux_baseline * 2.3)
    spectral_flux_spike_rate = _safe_div(flux_spike_count, len(flux_values))

    zcr_baseline = _quantile(zcr_values, 0.8) if zcr_values else 0.0
    zcr_spike_count = sum(1 for val in zcr_values if zcr_baseline > 0 and val > zcr_baseline * 1.9)
    zcr_spike_rate = _safe_div(zcr_spike_count, len(zcr_values))

    short_pause_break_rate = _safe_div(pause_count, max(1.0, duration_sec / 2.0))
    energy_cv = _safe_div(math.sqrt(_mean([(v - _mean(rms_values)) ** 2 for v in rms_values])), max(1e-6, _mean(rms_values))) if rms_values else 0.0
    pitch_cv = _safe_div(math.sqrt(_mean([(v - _mean(pitch_values)) ** 2 for v in pitch_values])), max(1e-6, _mean(pitch_values))) if pitch_values else 1.0
    pause_density = _safe_div(pause_count, max(duration_sec, 1e-6))
    voiced_segment_rate = _safe_div(voiced_segments, max(duration_sec, 1e-6))
    worst_artifact_window_sec = round((worst_rms_idx * hop_size) / float(sample_rate), 3) if rms_values else 0.0

    return {
        "duration_sec": round(duration_sec, 6),
        "f0_median_hz": round(f0_median_hz, 6),
        "f0_iqr_hz": round(f0_iqr_hz, 6),
        "voiced_ratio": round(voiced_ratio, 6),
        "pitch_jump_rate": round(pitch_jump_rate, 6),
        "pitch_dropout_rate": round(pitch_dropout_rate, 6),
        "rms_jump_rate": round(rms_jump_rate, 6),
        "spectral_flux_spike_rate": round(spectral_flux_spike_rate, 6),
        "zcr_spike_rate": round(zcr_spike_rate, 6),
        "clipping_ratio": round(clipping_ratio, 6),
        "short_pause_break_rate": round(short_pause_break_rate, 6),
        "energy_cv": round(energy_cv, 6),
        "pitch_cv": round(pitch_cv, 6),
        "pause_density": round(pause_density, 6),
        "voiced_segment_rate": round(voiced_segment_rate, 6),
        "worst_artifact_window_sec": worst_artifact_window_sec,
    }


def _build_reference_corpus_summary(reference_audio_paths: list[Path]) -> dict[str, Any]:
    pitch_profile_values: list[float] = []
    speaker_scores: list[float] = []
    durations: list[float] = []
    f0_medians: list[float] = []
    f0_iqrs: list[float] = []
    voiced_ratios: list[float] = []
    energy_cvs: list[float] = []
    pause_densities: list[float] = []
    voiced_segment_rates: list[float] = []

    per_file_features: list[dict[str, Any]] = []
    for path in reference_audio_paths:
        samples, sample_rate = _read_wav_mono_samples(path)
        features = _prosody_features_from_samples(samples, sample_rate)
        per_file_features.append(features)
        durations.append(float(features.get("duration_sec") or 0.0))
        f0_medians.append(float(features.get("f0_median_hz") or 0.0))
        f0_iqrs.append(float(features.get("f0_iqr_hz") or 0.0))
        voiced_ratios.append(float(features.get("voiced_ratio") or 0.0))
        energy_cvs.append(float(features.get("energy_cv") or 0.0))
        pause_densities.append(float(features.get("pause_density") or 0.0))
        voiced_segment_rates.append(float(features.get("voiced_segment_rate") or 0.0))

    return {
        "reference_count": len(reference_audio_paths),
        "duration_sec_mean": _pairwise_mean(durations) or 0.0,
        "f0_median_hz_mean": _pairwise_mean(f0_medians) or 0.0,
        "f0_iqr_hz_mean": _pairwise_mean(f0_iqrs) or 0.0,
        "voiced_ratio_mean": _pairwise_mean(voiced_ratios) or 0.0,
        "energy_cv_mean": _pairwise_mean(energy_cvs) or 0.0,
        "pause_density_mean": _pairwise_mean(pause_densities) or 0.0,
        "voiced_segment_rate_mean": _pairwise_mean(voiced_segment_rates) or 0.0,
        "per_file_features": per_file_features,
    }


def _resolve_reference_audio_paths(
    *,
    run_dir: Path,
    rec: dict[str, Any],
    explicit_local_path: str,
    explicit_s3_uri: str,
    reference_audio_cache_dir: str,
) -> tuple[list[Path], str, str]:
    paths: list[Path] = []
    source = ""
    reference_set_id = ""

    local_candidates = []
    if explicit_local_path:
        local_candidates.append(Path(explicit_local_path).expanduser())
    for key in ("reference_audio_local_path", "reference_audio_eval_path", "ref_audio_path"):
        raw = str(rec.get(key) or "").strip()
        if raw:
            local_candidates.append(Path(raw).expanduser())
    for candidate in local_candidates:
        if candidate.is_dir():
            found = sorted([p for p in candidate.iterdir() if p.suffix.lower() == ".wav"])
            if found:
                return found, "local_dir", candidate.name
        if candidate.exists() and candidate.is_file():
            return [candidate], "local_file", candidate.stem

    if explicit_s3_uri:
        try:
            import boto3  # type: ignore
        except Exception as e:
            raise RuntimeError(f"reference_audio_s3_uri requires boto3: {e}") from e
        if not explicit_s3_uri.startswith("s3://"):
            raise RuntimeError("reference_audio_s3_uri must start with s3://")
        parsed = urllib.parse.urlparse(explicit_s3_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        cache_root = Path(reference_audio_cache_dir).expanduser() if reference_audio_cache_dir else (run_dir / ".ref-audio-cache")
        cache_root.mkdir(parents=True, exist_ok=True)
        s3 = boto3.client("s3")
        manifest_key = key
        if not manifest_key.endswith(".json"):
            manifest_key = manifest_key.rstrip("/") + "/manifest.json"
        manifest_local = cache_root / hashlib.sha256(f"{bucket}/{manifest_key}".encode("utf-8")).hexdigest() / "manifest.json"
        manifest_local.parent.mkdir(parents=True, exist_ok=True)
        _download_s3_file_if_missing(s3, bucket, manifest_key, manifest_local)
        manifest = json.loads(manifest_local.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("reference manifest must be an object")
        reference_set_id = str(manifest.get("reference_set_id") or manifest.get("voice_id") or manifest_key.replace("/", "-")).strip()
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise RuntimeError("reference manifest files must be a non-empty list")
        local_files: list[Path] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            if item.get("enabled", True) is False:
                continue
            rel_key = str(item.get("key") or "").strip()
            if not rel_key:
                continue
            local_path = manifest_local.parent / rel_key.replace("/", "_")
            _download_s3_file_if_missing(s3, bucket, rel_key, local_path)
            if local_path.exists():
                local_files.append(local_path)
        if local_files:
            return local_files, "s3_manifest", reference_set_id or manifest_local.parent.name

    return paths, source, reference_set_id


def _analyze_audio_signal(audio_path: Path, *, reference_audio_paths: list[Path] | None = None) -> dict[str, Any]:
    analysis_started_at = time.perf_counter()
    samples, sample_rate = _read_wav_mono_samples(audio_path)
    if not samples or sample_rate <= 0:
        return {
            "duration_sec": 0.0,
            "mos_pred": None,
            "speaker_similarity": None,
            "pitch_profile_similarity": None,
            "f0_median_hz": 0.0,
            "f0_iqr_hz": 0.0,
            "voiced_ratio": 0.0,
            "pitch_jump_rate": 1.0,
            "pitch_dropout_rate": 1.0,
            "rms_jump_rate": 1.0,
            "spectral_flux_spike_rate": 1.0,
            "zcr_spike_rate": 1.0,
            "clipping_ratio": 1.0,
            "short_pause_break_rate": 1.0,
            "energy_cv": 1.0,
            "pitch_cv": 1.0,
            "pause_density": 1.0,
            "voiced_segment_rate": 1.0,
            "hard_artifact_fail": True,
            "hard_artifact_reason": "audio decode failed",
            "worst_artifact_window_sec": 0.0,
            "speaker_similarity_mean": None,
            "pitch_profile_similarity": None,
            "intonation_similarity": None,
            "reference_count": 0,
            "timings_ms": {},
            "capabilities": {
                "advanced_dsp_enabled": False,
                "mos_enabled": False,
                "speaker_similarity_enabled": False,
                "reference_corpus_loaded": False,
                "intonation_enabled": False,
            },
        }
    timings_ms: dict[str, float] = {}
    dsp_started_at = time.perf_counter()
    features = _prosody_features_from_samples(samples, sample_rate)
    timings_ms["dsp"] = round((time.perf_counter() - dsp_started_at) * 1000, 3)
    duration_sec = float(features.get("duration_sec") or 0.0)
    voiced_ratio = float(features.get("voiced_ratio") or 0.0)
    f0_median_hz = float(features.get("f0_median_hz") or 0.0)
    f0_iqr_hz = float(features.get("f0_iqr_hz") or 0.0)
    clipping_ratio = float(features.get("clipping_ratio") or 0.0)
    pitch_jump_rate = float(features.get("pitch_jump_rate") or 0.0)
    pitch_dropout_rate = float(features.get("pitch_dropout_rate") or 0.0)
    rms_jump_rate = float(features.get("rms_jump_rate") or 0.0)
    spectral_flux_spike_rate = float(features.get("spectral_flux_spike_rate") or 0.0)
    zcr_spike_rate = float(features.get("zcr_spike_rate") or 0.0)
    short_pause_break_rate = float(features.get("short_pause_break_rate") or 0.0)
    energy_cv = float(features.get("energy_cv") or 0.0)
    pitch_cv = float(features.get("pitch_cv") or 0.0)
    pause_density = float(features.get("pause_density") or 0.0)
    voiced_segment_rate = float(features.get("voiced_segment_rate") or 0.0)
    worst_artifact_window_sec = float(features.get("worst_artifact_window_sec") or 0.0)

    hard_artifact_reasons: list[str] = []
    if clipping_ratio >= 0.003:
        hard_artifact_reasons.append(f"clipping_ratio={clipping_ratio:.4f}")
    if rms_jump_rate >= 0.055 and spectral_flux_spike_rate >= 0.040:
        hard_artifact_reasons.append("energy/spectral spike")
    if pitch_jump_rate >= 0.22 and pitch_dropout_rate >= 0.45:
        hard_artifact_reasons.append("pitch discontinuity")
    if zcr_spike_rate >= 0.08 and spectral_flux_spike_rate >= 0.05:
        hard_artifact_reasons.append("transient burst")
    hard_artifact_fail = bool(hard_artifact_reasons)
    hard_artifact_reason = ", ".join(hard_artifact_reasons[:3])

    pitch_profile_similarity = None
    speaker_similarity = None
    speaker_similarity_mean = None
    intonation_similarity = None
    reference_count = 0
    capabilities = _seedlab_runtime_capabilities(reference_audio_paths=reference_audio_paths)
    capabilities["advanced_dsp_enabled"] = True
    reference_audio_paths = [p for p in (reference_audio_paths or []) if p.exists()]
    if reference_audio_paths:
        reference_count = len(reference_audio_paths)
        capabilities["reference_corpus_loaded"] = True
        capabilities["reference_count"] = reference_count
        reference_summary_started_at = time.perf_counter()
        corpus = _get_reference_corpus_summary(reference_audio_paths)
        timings_ms["reference_summary"] = round((time.perf_counter() - reference_summary_started_at) * 1000, 3)
        ref_median = float(corpus.get("f0_median_hz_mean") or 0.0)
        ref_iqr = float(corpus.get("f0_iqr_hz_mean") or 0.0)
        if ref_median > 0 and f0_median_hz > 0:
            pitch_delta = abs(math.log(max(f0_median_hz, 1.0) / max(ref_median, 1.0)))
            iqr_delta = abs(f0_iqr_hz - ref_iqr) / max(30.0, ref_iqr + 1e-6)
            pitch_profile_similarity = _clamp(1.0 - (pitch_delta / 0.7) - (iqr_delta * 0.3), 0.0, 1.0)
        intonation_parts = [
            _linear_similarity(voiced_ratio, ideal=float(corpus.get("voiced_ratio_mean") or voiced_ratio), tolerance=0.35),
            _linear_similarity(energy_cv, ideal=float(corpus.get("energy_cv_mean") or energy_cv), tolerance=0.80),
            _linear_similarity(pause_density, ideal=float(corpus.get("pause_density_mean") or pause_density), tolerance=0.9),
            _linear_similarity(voiced_segment_rate, ideal=float(corpus.get("voiced_segment_rate_mean") or voiced_segment_rate), tolerance=1.5),
            _linear_similarity(pitch_cv, ideal=_pairwise_mean([float(f.get("pitch_cv") or 0.0) for f in corpus.get("per_file_features", [])]) or pitch_cv, tolerance=0.75),
        ]
        intonation_similarity = _mean(intonation_parts)
        capabilities["intonation_enabled"] = True
        speaker_started_at = time.perf_counter()
        try:
            runtime = _resolve_seedlab_eval_runtime()
            resolved_device = str(runtime.get("resolved_device") or "cpu")
            speaker_similarity_mean, speaker_device = _predict_speaker_similarity(
                audio_path=audio_path,
                reference_audio_paths=reference_audio_paths,
                resolved_device=resolved_device,
            )
            speaker_similarity = speaker_similarity_mean
            capabilities["speaker_similarity_enabled"] = speaker_similarity_mean is not None
            capabilities["speaker_device"] = speaker_device
            if _torch_device_is_cuda(speaker_device):
                capabilities["gpu_acceleration_active"] = True
        except Exception as e:
            speaker_similarity = None
            capabilities["speaker_error"] = str(e)
        timings_ms["speaker_similarity"] = round((time.perf_counter() - speaker_started_at) * 1000, 3)

    mos_pred = None
    mos_started_at = time.perf_counter()
    try:
        runtime = _resolve_seedlab_eval_runtime()
        resolved_device = str(runtime.get("resolved_device") or "cpu")
        mos_pred, mos_device = _predict_distillmos_mos(audio_path, resolved_device)
        capabilities["mos_enabled"] = True
        capabilities["mos_device"] = mos_device
        if _torch_device_is_cuda(mos_device):
            capabilities["gpu_acceleration_active"] = True
    except Exception as e:
        mos_pred = None
        capabilities["mos_error"] = str(e)
    timings_ms["distillmos"] = round((time.perf_counter() - mos_started_at) * 1000, 3)
    timings_ms["total"] = round((time.perf_counter() - analysis_started_at) * 1000, 3)

    return {
        "duration_sec": round(duration_sec, 6),
        "mos_pred": mos_pred,
        "speaker_similarity": speaker_similarity,
        "speaker_similarity_mean": speaker_similarity_mean,
        "pitch_profile_similarity": pitch_profile_similarity,
        "intonation_similarity": round(float(intonation_similarity), 6) if isinstance(intonation_similarity, (int, float)) else None,
        "f0_median_hz": round(f0_median_hz, 6),
        "f0_iqr_hz": round(f0_iqr_hz, 6),
        "voiced_ratio": round(voiced_ratio, 6),
        "pitch_jump_rate": round(pitch_jump_rate, 6),
        "pitch_dropout_rate": round(pitch_dropout_rate, 6),
        "rms_jump_rate": round(rms_jump_rate, 6),
        "spectral_flux_spike_rate": round(spectral_flux_spike_rate, 6),
        "zcr_spike_rate": round(zcr_spike_rate, 6),
        "clipping_ratio": round(clipping_ratio, 6),
        "short_pause_break_rate": round(short_pause_break_rate, 6),
        "energy_cv": round(energy_cv, 6),
        "pitch_cv": round(pitch_cv, 6),
        "pause_density": round(pause_density, 6),
        "voiced_segment_rate": round(voiced_segment_rate, 6),
        "hard_artifact_fail": hard_artifact_fail,
        "hard_artifact_reason": hard_artifact_reason,
        "worst_artifact_window_sec": worst_artifact_window_sec,
        "reference_count": reference_count,
        "timings_ms": timings_ms,
        "capabilities": capabilities,
    }


def _score_from_ratio(value: float, *, good_min: float, good_max: float, soft_min: float, soft_max: float) -> str:
    if soft_min <= value <= soft_max:
        if good_min <= value <= good_max:
            return "5"
        return "4"
    if value < soft_min or value > soft_max:
        return "2"
    return "3"


def _compute_hybrid_scores(
    *,
    char_accuracy: float,
    length_ratio: float,
    chars_per_sec: float,
    signal: dict[str, Any],
) -> dict[str, Any]:
    capabilities = signal.get("capabilities") if isinstance(signal.get("capabilities"), dict) else {}
    mos_pred = signal.get("mos_pred")
    speaker_similarity = signal.get("speaker_similarity")
    pitch_profile_similarity = signal.get("pitch_profile_similarity")
    intonation_similarity_value = signal.get("intonation_similarity")

    pronunciation_raw = _clamp(_sigmoid01(char_accuracy, center=0.90, width=0.06), 0.0, 1.0)

    cps_lower = _sigmoid01(chars_per_sec, center=3.1, width=0.8)
    cps_upper = 1.0 - _sigmoid01(chars_per_sec, center=8.8, width=1.1)
    cps_score = _clamp(min(cps_lower, cps_upper) * 1.15, 0.0, 1.0)
    length_stability = _linear_similarity(length_ratio, ideal=1.0, tolerance=0.32)
    if isinstance(mos_pred, (int, float)):
        mos_score = _clamp((float(mos_pred) - 1.5) / 3.5, 0.0, 1.0)
        naturalness_raw = _clamp(mos_score * 0.7 + cps_score * 0.15 + length_stability * 0.15, 0.0, 1.0)
    else:
        naturalness_raw = _clamp(cps_score * 0.55 + length_stability * 0.45, 0.0, 1.0)

    dropout_penalty = _clamp(float(signal.get("pitch_dropout_rate") or 0.0) * 1.45, 0.0, 1.0)
    rms_penalty = _clamp(float(signal.get("rms_jump_rate") or 0.0) * 10.0, 0.0, 1.0)
    pause_penalty = _clamp(float(signal.get("short_pause_break_rate") or 0.0) / 0.65, 0.0, 1.0)
    length_penalty = 1.0 - length_stability
    stability_raw = _clamp(1.0 - (dropout_penalty * 0.40 + rms_penalty * 0.25 + pause_penalty * 0.15 + length_penalty * 0.20), 0.0, 1.0)

    tone_parts: list[float] = []
    if isinstance(speaker_similarity, (int, float)):
        tone_parts.append(_clamp((float(speaker_similarity) + 1.0) / 2.0, 0.0, 1.0))
    if isinstance(pitch_profile_similarity, (int, float)):
        tone_parts.append(_clamp(float(pitch_profile_similarity), 0.0, 1.0))
    tone_fit_raw = _pairwise_mean(tone_parts)

    pitch_jump_penalty = _clamp(float(signal.get("pitch_jump_rate") or 0.0) / 0.30, 0.0, 1.0)
    pitch_dropout_penalty = _clamp(float(signal.get("pitch_dropout_rate") or 0.0) / 0.55, 0.0, 1.0)
    pitch_cv_penalty = _clamp(float(signal.get("pitch_cv") or 0.0) / 1.20, 0.0, 1.0)
    pitch_consistency_raw = _clamp(1.0 - (pitch_jump_penalty * 0.45 + pitch_dropout_penalty * 0.40 + pitch_cv_penalty * 0.15), 0.0, 1.0)

    clipping_penalty = _clamp(float(signal.get("clipping_ratio") or 0.0) / 0.003, 0.0, 1.0)
    flux_penalty = _clamp(float(signal.get("spectral_flux_spike_rate") or 0.0) / 0.08, 0.0, 1.0)
    zcr_penalty = _clamp(float(signal.get("zcr_spike_rate") or 0.0) / 0.10, 0.0, 1.0)
    artifact_rms_penalty = _clamp(float(signal.get("rms_jump_rate") or 0.0) / 0.08, 0.0, 1.0)
    artifact_cleanliness_raw = _clamp(1.0 - (clipping_penalty * 0.35 + flux_penalty * 0.25 + zcr_penalty * 0.15 + artifact_rms_penalty * 0.25), 0.0, 1.0)

    intonation_similarity_raw = _clamp(float(intonation_similarity_value), 0.0, 1.0) if isinstance(intonation_similarity_value, (int, float)) else None

    hard_artifact_fail = bool(signal.get("hard_artifact_fail"))
    hard_artifact_reason = str(signal.get("hard_artifact_reason") or "").strip()

    prosody_fail = False
    prosody_fail_reason = ""
    if intonation_similarity_raw is not None and tone_fit_raw is not None:
        if intonation_similarity_raw < 0.28 and tone_fit_raw < 0.34:
            prosody_fail = True
            prosody_fail_reason = (
                f"intonation_similarity={intonation_similarity_raw:.2f}, "
                f"tone_fit={tone_fit_raw:.2f}"
            )

    if hard_artifact_fail:
        artifact_cleanliness_raw = min(artifact_cleanliness_raw, 0.02)
        stability_raw = min(stability_raw, 0.25)

    raw_scores: dict[str, float | None] = {
        "naturalness_raw": naturalness_raw,
        "pronunciation_raw": pronunciation_raw,
        "stability_raw": stability_raw,
        "tone_fit_raw": tone_fit_raw,
        "pitch_consistency_raw": pitch_consistency_raw,
        "artifact_cleanliness_raw": artifact_cleanliness_raw,
        "intonation_similarity_raw": intonation_similarity_raw,
    }
    raw_weights: dict[str, float] = {
        "naturalness_raw": 0.20,
        "pronunciation_raw": 0.18,
        "stability_raw": 0.17,
        "tone_fit_raw": 0.15,
        "pitch_consistency_raw": 0.10,
        "artifact_cleanliness_raw": 0.10,
        "intonation_similarity_raw": 0.10,
    }
    available_weight = sum(weight for key, weight in raw_weights.items() if isinstance(raw_scores.get(key), (int, float)))
    if available_weight > 0:
        weighted_ai_score_raw = sum(float(raw_scores[key]) * weight for key, weight in raw_weights.items() if isinstance(raw_scores.get(key), (int, float))) / available_weight
    else:
        weighted_ai_score_raw = 0.0

    naturalness = _map_similarity_to_score(naturalness_raw)
    pronunciation = _map_similarity_to_score(pronunciation_raw)
    stability = _map_similarity_to_score(stability_raw)
    tone_fit = _map_similarity_to_score(tone_fit_raw)
    pitch_consistency = _map_similarity_to_score(pitch_consistency_raw)
    artifact_cleanliness = _map_similarity_to_score(artifact_cleanliness_raw)
    intonation_similarity = _map_similarity_to_score(intonation_similarity_raw)
    weighted_ai_score = _map_similarity_to_score(weighted_ai_score_raw)

    rank_excluded = hard_artifact_fail or prosody_fail

    return {
        "naturalness": naturalness,
        "pronunciation": pronunciation,
        "stability": stability,
        "tone_fit": tone_fit,
        "pitch_consistency": pitch_consistency,
        "artifact_cleanliness": artifact_cleanliness,
        "intonation_similarity": intonation_similarity,
        "naturalness_raw": round(float(naturalness_raw), 6),
        "pronunciation_raw": round(float(pronunciation_raw), 6),
        "stability_raw": round(float(stability_raw), 6),
        "tone_fit_raw": round(float(tone_fit_raw), 6) if isinstance(tone_fit_raw, (int, float)) else None,
        "pitch_consistency_raw": round(float(pitch_consistency_raw), 6),
        "artifact_cleanliness_raw": round(float(artifact_cleanliness_raw), 6),
        "intonation_similarity_raw": round(float(intonation_similarity_raw), 6) if isinstance(intonation_similarity_raw, (int, float)) else None,
        "weighted_ai_score_raw": round(float(weighted_ai_score_raw), 6),
        "weighted_ai_score": weighted_ai_score,
        "hard_artifact_fail": hard_artifact_fail,
        "hard_artifact_reason": hard_artifact_reason,
        "prosody_fail": prosody_fail,
        "prosody_fail_reason": prosody_fail_reason,
        "rank_excluded": rank_excluded,
        "capabilities": capabilities,
    }


def _coerce_score(value: Any) -> str:
    try:
        num = int(str(value).strip())
    except Exception:
        return ""
    if num < 1:
        num = 1
    if num > 5:
        num = 5
    return str(num)


def _cap_score(score: str, max_score: int) -> str:
    if not score:
        return ""
    try:
        num = int(score)
    except Exception:
        return ""
    return str(min(num, max(1, max_score)))


def _empty_eval(note: str, *, status: str) -> dict[str, Any]:
    return {
        "naturalness": "",
        "pronunciation": "",
        "stability": "",
        "tone_fit": "",
        "pitch_consistency": "",
        "artifact_cleanliness": "",
        "intonation_similarity": "",
        "weighted_ai_score": "",
        "weighted_ai_score_raw": "",
        "hard_artifact_fail": False,
        "hard_artifact_reason": "",
        "prosody_fail": False,
        "prosody_fail_reason": "",
        "rank_excluded": False,
        "capabilities": {},
        "executor": "",
        "reference_set_id": "",
        "note": note[:600],
        "selected": False,
        "updated_at": dt.datetime.now().isoformat(),
        "auto_eval_status": status,
    }


def _openai_transcribe_audio(
    *,
    api_key: str,
    audio_path: Path,
    asr_model: str,
    language: str,
    timeout_seconds: int,
) -> tuple[str, dict[str, Any]]:
    payload = _http_post_multipart(
        "https://api.openai.com/v1/audio/transcriptions",
        fields={
            "model": asr_model,
            "response_format": "json",
            "language": language,
        },
        file_field="file",
        file_path=audio_path,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout_seconds=timeout_seconds,
    )
    text = str(payload.get("text") or "").strip()
    if text:
        usage = {
            "model": asr_model,
            "language": language,
            "audio_size_bytes": int(audio_path.stat().st_size) if audio_path.exists() else 0,
            "audio_duration_sec": round(_wav_duration_seconds(audio_path), 6),
        }
        return text, usage
    raise RuntimeError("empty transcript")


def _openai_generate_eval_note(
    *,
    api_key: str,
    judge_model: str,
    timeout_seconds: int,
    script_text: str,
    transcript_text: str,
    metrics: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    system_prompt = (
        "당신은 한국어 TTS 품질 평가자다. "
        "반드시 note 하나만 JSON 객체로 반환한다. "
        "키는 note 이다."
    )
    user_prompt = (
        "아래 정보를 보고 TTS 품질 평가 코멘트를 작성하라.\n"
        f"- 기준 대본:\n{script_text}\n\n"
        f"- ASR 전사:\n{transcript_text}\n\n"
        f"- 자동 지표(JSON):\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n\n"
        "주의:\n"
        "- 점수는 이미 계산되었다. 새 점수를 만들지 말고 note만 작성한다.\n"
        "- 1~2문장으로, 자연스러움/톤/피치/튐 관점의 핵심 문제를 요약한다.\n"
        "- hard_artifact_fail=true면 그 원인을 먼저 말한다.\n"
    )
    payload = {
        "model": judge_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response = _http_post_json(
        "https://api.openai.com/v1/chat/completions",
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout_seconds=timeout_seconds,
    )
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("judge returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "").strip()
    parsed = _extract_json_object(content)
    if not parsed:
        raise RuntimeError("judge returned non-json content")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return str(parsed.get("note") or "").strip(), {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "model": judge_model,
    }


def _resolve_reference_audio_path(
    *,
    run_dir: Path,
    rec: dict[str, Any],
    explicit_local_path: str,
    explicit_s3_uri: str,
    reference_audio_cache_dir: str,
) -> tuple[Path | None, str]:
    local_candidate = str(explicit_local_path or "").strip()
    if local_candidate:
        path = Path(local_candidate).expanduser()
        if not path.is_absolute():
            path = (run_dir / path).resolve()
        if path.exists():
            return path, "local"
        raise RuntimeError(f"reference audio not found: {path}")

    if explicit_s3_uri.strip():
        try:
            import boto3  # type: ignore
        except Exception as e:
            raise RuntimeError(f"reference_audio_s3_uri requires boto3: {e}") from e
        uri = explicit_s3_uri.strip()
        if not uri.startswith("s3://"):
            raise RuntimeError("reference_audio_s3_uri must start with s3://")
        bucket_and_key = uri[5:]
        if "/" not in bucket_and_key:
            raise RuntimeError("reference_audio_s3_uri missing key")
        bucket, key = bucket_and_key.split("/", 1)
        cache_root = Path(reference_audio_cache_dir).expanduser() if reference_audio_cache_dir else (run_dir / ".ref-audio-cache")
        cache_root.mkdir(parents=True, exist_ok=True)
        target = cache_root / Path(key).name
        if not target.exists():
            boto3.client("s3").download_file(bucket, key, str(target))
        return target, "s3"

    tts_params = rec.get("tts_params") if isinstance(rec.get("tts_params"), dict) else {}
    for key in ("reference_audio_local_path", "reference_audio_eval_path", "ref_audio_path"):
        candidate = str(tts_params.get(key) or "").strip()
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = (run_dir / path).resolve()
        if path.exists():
            return path, f"tts_params:{key}"
    return None, ""


def _auto_eval_single_record(
    *,
    run_dir: Path,
    rec: dict[str, Any],
    asr_api_key: str,
    judge_api_key: str,
    asr_model: str,
    judge_model: str,
    language: str,
    timeout_seconds: int,
    evaluation_profile: str,
    reference_audio_local_path: str,
    reference_audio_s3_uri: str,
    reference_audio_cache_dir: str,
    disable_llm_note: bool,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    sample_id = str(rec.get("sample_id") or "").strip()
    seed = int(rec.get("seed") or 0)
    script_id = str(rec.get("script_id") or "")
    script_text = str(rec.get("script_text") or "")
    audio_rel_path = str(rec.get("audio_rel_path") or "").strip()
    audio_path = (run_dir / audio_rel_path).resolve()

    if not sample_id:
        raise RuntimeError("record missing sample_id")
    if not audio_rel_path or not audio_path.exists():
        eval_obj = _empty_eval("AUTO-EVAL FAILED: audio file missing", status="failed")
        debug = {
            "sample_id": sample_id,
            "seed": seed,
            "script_id": script_id,
            "status": "failed",
            "error": "audio file missing",
        }
        return sample_id, eval_obj, debug

    return _auto_eval_audio_file(
        run_dir=run_dir,
        sample_id=sample_id,
        seed=seed,
        script_id=script_id,
        script_text=script_text,
        audio_path=audio_path,
        rec=rec,
        asr_api_key=asr_api_key,
        judge_api_key=judge_api_key,
        asr_model=asr_model,
        judge_model=judge_model,
        language=language,
        timeout_seconds=timeout_seconds,
        evaluation_profile=evaluation_profile,
        reference_audio_local_path=reference_audio_local_path,
        reference_audio_s3_uri=reference_audio_s3_uri,
        reference_audio_cache_dir=reference_audio_cache_dir,
        disable_llm_note=disable_llm_note,
    )


def _auto_eval_audio_file(
    *,
    run_dir: Path,
    sample_id: str,
    seed: int,
    script_id: str,
    script_text: str,
    audio_path: Path,
    rec: dict[str, Any],
    asr_api_key: str,
    judge_api_key: str,
    asr_model: str,
    judge_model: str,
    language: str,
    timeout_seconds: int,
    evaluation_profile: str,
    reference_audio_local_path: str,
    reference_audio_s3_uri: str,
    reference_audio_cache_dir: str,
    disable_llm_note: bool,
    executor: str = "local",
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    started = time.time()
    try:
        stage_timings_ms: dict[str, float] = {}
        reference_started_at = time.perf_counter()
        reference_audio_paths, reference_audio_source, reference_set_id = _resolve_reference_audio_paths(
            run_dir=run_dir,
            rec=rec,
            explicit_local_path=reference_audio_local_path,
            explicit_s3_uri=reference_audio_s3_uri,
            reference_audio_cache_dir=reference_audio_cache_dir,
        )
        stage_timings_ms["reference_load"] = round((time.perf_counter() - reference_started_at) * 1000, 3)

        asr_started_at = time.perf_counter()
        transcript_text, asr_usage = _openai_transcribe_audio(
            api_key=asr_api_key,
            audio_path=audio_path,
            asr_model=asr_model,
            language=language,
            timeout_seconds=timeout_seconds,
        )
        stage_timings_ms["asr"] = round((time.perf_counter() - asr_started_at) * 1000, 3)
        ref_norm = _normalize_compare_text(script_text)
        hyp_norm = _normalize_compare_text(transcript_text)
        char_acc = _char_accuracy(ref_norm, hyp_norm)
        length_ratio = (len(hyp_norm) / len(ref_norm)) if ref_norm else 0.0
        duration = _wav_duration_seconds(audio_path)
        chars_per_sec = (len(hyp_norm) / duration) if duration > 0 else 0.0

        signal_started_at = time.perf_counter()
        signal = _analyze_audio_signal(audio_path, reference_audio_paths=reference_audio_paths)
        stage_timings_ms["signal_analysis"] = round((time.perf_counter() - signal_started_at) * 1000, 3)
        signal_timings = signal.get("timings_ms") if isinstance(signal.get("timings_ms"), dict) else {}
        for key, value in signal_timings.items():
            if isinstance(value, (int, float)):
                stage_timings_ms[f"signal_{key}"] = round(float(value), 3)
        scored = _compute_hybrid_scores(
            char_accuracy=char_acc,
            length_ratio=length_ratio,
            chars_per_sec=chars_per_sec,
            signal=signal,
        )
        note_metrics = {
            "char_accuracy": round(char_acc, 6),
            "length_ratio": round(length_ratio, 6),
            "chars_per_sec": round(chars_per_sec, 6),
            **{key: scored.get(key) for key in AI_SCORE_KEYS},
            "weighted_ai_score": scored.get("weighted_ai_score"),
            "hard_artifact_fail": scored.get("hard_artifact_fail"),
            "hard_artifact_reason": scored.get("hard_artifact_reason"),
            "signal": signal,
        }
        raw_note = ""
        judge_usage: dict[str, Any] = {}
        note_started_at = time.perf_counter()
        if not disable_llm_note:
            try:
                raw_note, judge_usage = _openai_generate_eval_note(
                    api_key=judge_api_key,
                    judge_model=judge_model,
                    timeout_seconds=timeout_seconds,
                    script_text=script_text,
                    transcript_text=transcript_text,
                    metrics=note_metrics,
                )
            except Exception as note_err:
                raw_note = f"LLM note unavailable: {note_err}"
        stage_timings_ms["judge_note"] = round((time.perf_counter() - note_started_at) * 1000, 3)
        stage_timings_ms["total"] = round((time.time() - started) * 1000, 3)
        metrics_note = f"acc={char_acc:.3f}, len={length_ratio:.2f}, cps={chars_per_sec:.2f}"
        note = f"[AI] {metrics_note} | {raw_note}".strip()

        capabilities = scored.get("capabilities") or signal.get("capabilities") or {}
        eval_obj = {
            "naturalness": scored["naturalness"],
            "pronunciation": scored["pronunciation"],
            "stability": scored["stability"],
            "tone_fit": scored["tone_fit"],
            "pitch_consistency": scored["pitch_consistency"],
            "artifact_cleanliness": scored["artifact_cleanliness"],
            "intonation_similarity": scored["intonation_similarity"],
            "weighted_ai_score": scored["weighted_ai_score"],
            "weighted_ai_score_raw": scored["weighted_ai_score_raw"],
            "hard_artifact_fail": scored["hard_artifact_fail"],
            "hard_artifact_reason": scored["hard_artifact_reason"],
            "prosody_fail": scored["prosody_fail"],
            "prosody_fail_reason": scored["prosody_fail_reason"],
            "rank_excluded": scored["rank_excluded"],
            "capabilities": capabilities,
            "executor": executor,
            "reference_set_id": reference_set_id,
            "note": note[:600],
            "selected": False,
            "updated_at": dt.datetime.now().isoformat(),
            "auto_eval_status": "ready",
        }
        debug = {
            "sample_id": sample_id,
            "seed": seed,
            "script_id": script_id,
            "status": "ready",
            "evaluation_profile": evaluation_profile,
            "asr_model": asr_model,
            "judge_model": judge_model,
            "transcript_text": transcript_text,
            "char_accuracy": round(char_acc, 6),
            "length_ratio": round(length_ratio, 6),
            "chars_per_sec": round(chars_per_sec, 6),
            "duration_sec": round(duration, 6),
            "reference_audio_source": reference_audio_source,
            "reference_audio_paths": [str(path) for path in reference_audio_paths],
            "reference_set_id": reference_set_id,
            "executor": executor,
            "resolved_device": (capabilities or {}).get("resolved_device"),
            "gpu_acceleration_active": bool((capabilities or {}).get("gpu_acceleration_active")),
            "stage_timings_ms": stage_timings_ms,
            "remote_eval": {
                "executor": executor,
                "gpu_acceleration_active": bool((capabilities or {}).get("gpu_acceleration_active")),
                "status": "completed",
            },
            "cost_tracking": {
                "seedlab_asr": dict(asr_usage or {}),
                "seedlab_judge": dict(judge_usage or {}),
            },
            "signal_metrics": signal,
            "scored_metrics": scored,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        return sample_id, eval_obj, debug
    except Exception as e:
        msg = str(e).strip() or "auto eval failed"
        eval_obj = _empty_eval(f"AUTO-EVAL FAILED: {msg}", status="failed")
        eval_obj["executor"] = executor
        debug = {
            "sample_id": sample_id,
            "seed": seed,
            "script_id": script_id,
            "status": "failed",
            "executor": executor,
            "error": msg,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        return sample_id, eval_obj, debug


def cmd_auto_eval(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("records")
    if not isinstance(records, list):
        raise RuntimeError("manifest.records missing")

    ready_records: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("status") not in ("ready", "skipped_existing"):
            continue
        if str(rec.get("audio_rel_path") or "").strip():
            ready_records.append(rec)
    if not ready_records:
        raise RuntimeError("no ready records found in manifest")

    asr_api_key, judge_api_key = _resolve_openai_keys(
        explicit_shared_key=str(args.openai_api_key),
        explicit_asr_key=str(args.openai_api_key_asr),
        explicit_judge_key=str(args.openai_api_key_judge),
    )
    asr_model_requested = str(args.asr_model)
    asr_model_resolved, asr_warning = _resolve_asr_model_for_transcription(asr_model_requested)
    if asr_warning:
        print(f"[seed-lab] WARN: {asr_warning}", flush=True)
    judge_model = str(args.judge_model)
    out_json = Path(args.out_json).resolve() if args.out_json else (run_dir / "auto_eval.json")
    debug_jsonl = run_dir / "auto_eval_debug.jsonl"

    eval_map: dict[str, dict[str, Any]] = {}
    debug_rows: list[dict[str, Any]] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as pool:
        futures: list[concurrent.futures.Future[tuple[str, dict[str, Any], dict[str, Any]]]] = []
        for rec in ready_records:
            futures.append(
                pool.submit(
                    _auto_eval_single_record,
                    run_dir=run_dir,
                    rec=rec,
                    asr_api_key=asr_api_key,
                    judge_api_key=judge_api_key,
                    asr_model=asr_model_resolved,
                    judge_model=judge_model,
                    language=str(args.language),
                    timeout_seconds=int(args.timeout),
                    evaluation_profile=str(args.evaluation_profile),
                    reference_audio_local_path=str(args.reference_audio_local_path),
                    reference_audio_s3_uri=str(args.reference_audio_s3_uri),
                    reference_audio_cache_dir=str(args.reference_audio_cache_dir),
                    disable_llm_note=bool(args.disable_llm_note),
                )
            )
        for fut in concurrent.futures.as_completed(futures):
            sample_id, eval_obj, debug_obj = fut.result()
            eval_map[sample_id] = eval_obj
            debug_rows.append(debug_obj)
            done += 1
            if done % 10 == 0 or done == len(futures):
                print(f"[seed-lab] auto-eval progress {done}/{len(futures)}", flush=True)

    payload = {
        "run_id": str((manifest.get("meta") or {}).get("run_id") or run_dir.name),
        "exported_at": dt.datetime.now().isoformat(),
        "mode": "auto_eval_hybrid_v3",
        "evaluation_profile": str(args.evaluation_profile),
        "asr_model": asr_model_resolved,
        "asr_model_requested": asr_model_requested,
        "judge_model": judge_model,
        "evaluations": eval_map,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with debug_jsonl.open("w", encoding="utf-8") as fp:
        for row in debug_rows:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")

    success_count = sum(1 for v in eval_map.values() if str(v.get("auto_eval_status")) == "ready")
    fail_count = len(eval_map) - success_count
    print("")
    print(f"[seed-lab] run_dir={run_dir}")
    print(f"[seed-lab] auto_eval_json={out_json}")
    print(f"[seed-lab] auto_eval_debug={debug_jsonl}")
    print(f"[seed-lab] asr_model={asr_model_resolved} (requested={asr_model_requested})")
    print(f"[seed-lab] judge_model={judge_model}")
    print(f"[seed-lab] evaluation_profile={args.evaluation_profile}")
    print(f"[seed-lab] auto_eval_ready={success_count} failed={fail_count} total={len(eval_map)}")
    print("")
    print("[next]")
    print(
        "report 생성: "
        f"python3 scripts/seed_lab.py report --run-dir {sh_escape(str(run_dir))} --eval-json {sh_escape(str(out_json))} --top {int(args.top)}"
    )
    return 0


def _load_manifest(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"manifest not found: {manifest_path}")
    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("manifest must be object")
    meta = parsed.get("meta")
    records = parsed.get("records")
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(records, list):
        raise RuntimeError("manifest.records missing")
    out_records = [r for r in records if isinstance(r, dict)]
    return meta, out_records


def _resolve_serve_default_tts_params(meta: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    dataset_path_raw = str(meta.get("dataset") or "").strip()
    if not dataset_path_raw:
        return {}
    dataset_path = Path(dataset_path_raw)
    if not dataset_path.is_absolute():
        dataset_path = (run_dir / dataset_path).resolve()
    if not dataset_path.exists():
        return {}
    try:
        _scripts, tts_params = load_dataset(dataset_path)
        return dict(tts_params)
    except Exception:
        return {}


def _normalize_record_for_ui(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    rel = str(out.get("audio_rel_path") or "").strip().replace("\\", "/")
    if rel:
        out["audio_rel_path"] = rel
        out["audio_url"] = _audio_url_from_rel_path(rel)
    return out


def cmd_serve(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        raise RuntimeError(f"run dir not found: {run_dir}")
    meta, manifest_records = _load_manifest(run_dir)
    run_id = str(meta.get("run_id") or run_dir.name)
    endpoint = _resolve_api_endpoint(args.api_url or os.getenv("TTS_API_URL", ""))
    default_tts_params = _resolve_serve_default_tts_params(meta, run_dir)
    manifest_by_id: dict[str, dict[str, Any]] = {}
    for rec in manifest_records:
        sample_id = str(rec.get("sample_id") or "").strip()
        if sample_id:
            manifest_by_id[sample_id] = rec

    asr_api_key = ""
    judge_api_key = ""
    openai_configured = False
    try:
        asr_api_key, judge_api_key = _resolve_openai_keys(
            explicit_shared_key=str(args.openai_api_key),
            explicit_asr_key=str(args.openai_api_key_asr),
            explicit_judge_key=str(args.openai_api_key_judge),
        )
        openai_configured = True
    except Exception:
        openai_configured = False
    asr_model_requested = str(args.asr_model)
    asr_model_resolved, asr_warning = _resolve_asr_model_for_transcription(asr_model_requested)
    judge_model = str(args.judge_model)
    auto_eval_on_add = not bool(args.disable_auto_eval_on_add)
    live_records_path = run_dir / LIVE_RECORDS_JSONL
    live_eval_path = run_dir / LIVE_AUTO_EVAL_JSON
    live_eval_debug_path = run_dir / "auto_eval_live_debug.jsonl"
    base_eval_path = run_dir / "auto_eval.json"
    human_eval_path = run_dir / HUMAN_EVAL_JSON
    write_lock = threading.Lock()

    class SeedLabHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    class SeedLabHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *handler_args: Any, **handler_kwargs: Any) -> None:
            super().__init__(*handler_args, directory=str(run_dir), **handler_kwargs)

        def log_message(self, fmt: str, *fmt_args: Any) -> None:  # noqa: A003
            now = dt.datetime.now().strftime("%H:%M:%S")
            sys.stdout.write(f"[seed-lab serve {now}] {fmt % fmt_args}\n")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", "ignore")
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise RuntimeError("body must be json object")
            return parsed

        def _resolve_record_by_sample_id(self, sample_id: str) -> dict[str, Any] | None:
            if sample_id in manifest_by_id:
                return manifest_by_id[sample_id]
            live_rows = _read_jsonl_objects(live_records_path)
            for row in reversed(live_rows):
                if str(row.get("sample_id") or "") == sample_id:
                    return row
            return None

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            if route in ("", "/"):
                self.path = "/index.html"
                return super().do_GET()

            if route == "/api/live-records":
                rows = [_normalize_record_for_ui(r) for r in _read_jsonl_objects(live_records_path)]
                rows.sort(key=lambda r: str(r.get("created_at") or ""))
                return self._send_json(200, {"run_id": run_id, "records": rows})

            if route == "/api/ai-evals":
                merged = _merge_eval_maps([base_eval_path, live_eval_path])
                return self._send_json(200, {"run_id": run_id, "evaluations": merged})

            if route == "/api/human-evals":
                merged = _load_human_eval_map(human_eval_path)
                return self._send_json(200, {"run_id": run_id, "evaluations": merged})

            if route == "/api/health":
                return self._send_json(
                    200,
                    {
                        "ok": True,
                        "run_id": run_id,
                        "tts_endpoint": endpoint,
                        "auto_eval_on_add": auto_eval_on_add,
                        "openai_configured": openai_configured,
                    },
                )

            if route == "/api/config":
                return self._send_json(
                    200,
                    {
                        "run_id": run_id,
                        "tts_endpoint": endpoint,
                        "default_tts_params": default_tts_params,
                        "auto_eval_on_add": auto_eval_on_add,
                        "openai_configured": openai_configured,
                        "asr_model": asr_model_resolved,
                        "asr_model_requested": asr_model_requested,
                        "judge_model": judge_model,
                        "evaluation_profile": str(args.evaluation_profile),
                    },
                )

            if route == "/api/run-status":
                rows = _read_jsonl_objects(live_records_path)
                merged_evals = _merge_eval_maps([base_eval_path, live_eval_path])
                executor_counts: dict[str, int] = {}
                gpu_active_sample_count = 0
                remote_eval_failed_count = 0
                avg_stage_timings_ms: dict[str, float] = {}
                timing_totals: dict[str, float] = {}
                timing_samples = 0
                for debug_row in _read_jsonl_objects(live_eval_debug_path):
                    if not isinstance(debug_row, dict):
                        continue
                    executor = str(debug_row.get("executor") or "").strip()
                    if executor:
                        executor_counts[executor] = int(executor_counts.get(executor) or 0) + 1
                    if bool(debug_row.get("gpu_acceleration_active")):
                        gpu_active_sample_count += 1
                    remote_eval = debug_row.get("remote_eval") if isinstance(debug_row.get("remote_eval"), dict) else {}
                    if str(remote_eval.get("status") or "").strip().lower() == "failed":
                        remote_eval_failed_count += 1
                    timings = debug_row.get("stage_timings_ms") if isinstance(debug_row.get("stage_timings_ms"), dict) else {}
                    if timings:
                        timing_samples += 1
                        for key, value in timings.items():
                            if isinstance(value, (int, float)):
                                timing_totals[key] = float(timing_totals.get(key) or 0.0) + float(value)
                if timing_samples > 0:
                    avg_stage_timings_ms = {
                        key: round(total / timing_samples, 1)
                        for key, total in timing_totals.items()
                    }
                return self._send_json(
                    200,
                    {
                        "run_id": run_id,
                        "status": "ready",
                        "stage": "ready",
                        "generated_count": len(rows) or len(manifest_records),
                        "failed_count": sum(1 for row in rows if str(row.get("status") or "") == "failed"),
                        "evaluated_count": len(merged_evals),
                        "total_count": len(manifest_records) or len(rows),
                        "eval_failed_count": sum(
                            1 for value in merged_evals.values()
                            if isinstance(value, dict) and str(value.get("auto_eval_status") or "").strip().lower() != "ready"
                        ),
                        "runpod_job_count": int(executor_counts.get("runpod_gpu") or 0),
                        "gpu_active_sample_count": gpu_active_sample_count,
                        "remote_eval_failed_count": remote_eval_failed_count,
                        "remote_eval_last_error": "",
                        "eval_executor_counts": executor_counts,
                        "avg_stage_timings_ms": avg_stage_timings_ms,
                        "last_error": "",
                    },
                )

            self.path = route
            return super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path

            if route == "/api/tts/generate":
                try:
                    body = self._read_json()
                    script_text = str(body.get("script_text") or "").strip()
                    if not script_text:
                        raise RuntimeError("script_text is required")

                    seed_raw = str(body.get("seed") or "").strip()
                    if seed_raw:
                        seed = _to_int(seed_raw, default=1, min_value=1, max_value=SEED_MAX)
                    else:
                        seed = random.randint(1, SEED_MAX)

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
                        tts_params["fragment_interval"] = _to_float(
                            body.get("fragment_interval"),
                            default=0.4,
                            min_value=0.0,
                            max_value=5.0,
                        )
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

                    payload = _build_payload(script_text, tts_params, seed=seed)
                    audio_bytes = _http_post_tts(endpoint, payload, timeout_seconds=int(args.timeout))
                    if not audio_bytes:
                        raise RuntimeError("empty response from tts")

                    created = dt.datetime.now()
                    live_dir = run_dir / "audio" / "live"
                    live_dir.mkdir(parents=True, exist_ok=True)
                    audio_name = f"seed-{seed}-{created.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.wav"
                    audio_path = live_dir / audio_name
                    audio_path.write_bytes(audio_bytes)

                    sample_id = f"live:{seed}:{int(created.timestamp() * 1000)}"
                    audio_rel = os.path.relpath(audio_path, run_dir).replace("\\", "/")
                    record = {
                        "sample_id": sample_id,
                        "seed": seed,
                        "take_index": 1,
                        "script_id": str(body.get("script_id") or "live"),
                        "script_title": str(body.get("script_title") or "즉석 생성"),
                        "script_text": script_text,
                        "audio_rel_path": audio_rel,
                        "audio_url": _audio_url_from_rel_path(audio_rel),
                        "status": "ready",
                        "error_type": "",
                        "error": "",
                        "bytes": len(audio_bytes),
                        "created_at": created.isoformat(),
                        "tts_params": {
                            "ref_audio_path": str(tts_params.get("ref_audio_path") or ""),
                            "reference_audio_local_path": str(tts_params.get("reference_audio_local_path") or ""),
                        },
                    }

                    add_to_review = _to_bool(body.get("add_to_review"), default=False)
                    ai_eval_obj: dict[str, Any] | None = None
                    ai_debug_obj: dict[str, Any] | None = None

                    if add_to_review:
                        with write_lock:
                            _append_jsonl_object(live_records_path, record)

                        if auto_eval_on_add and openai_configured:
                            sample_id_eval, eval_obj, debug_obj = _auto_eval_single_record(
                                run_dir=run_dir,
                                rec=record,
                                asr_api_key=asr_api_key,
                                judge_api_key=judge_api_key,
                                asr_model=asr_model_resolved,
                                judge_model=judge_model,
                                language=str(args.language),
                                timeout_seconds=int(args.auto_eval_timeout),
                                evaluation_profile=str(args.evaluation_profile),
                                reference_audio_local_path=str(args.reference_audio_local_path),
                                reference_audio_s3_uri=str(args.reference_audio_s3_uri),
                                reference_audio_cache_dir=str(args.reference_audio_cache_dir),
                                disable_llm_note=bool(args.disable_llm_note),
                            )
                            ai_eval_obj = eval_obj
                            ai_debug_obj = debug_obj
                            with write_lock:
                                _upsert_eval_entry(
                                    live_eval_path,
                                    run_id=run_id,
                                    sample_id=sample_id_eval,
                                    eval_obj=eval_obj,
                                    asr_model=asr_model_resolved,
                                    judge_model=judge_model,
                                )
                                _append_jsonl_object(live_eval_debug_path, debug_obj)

                    return self._send_json(
                        200,
                        {
                            "ok": True,
                            "run_id": run_id,
                            "seed": seed,
                            "audio_url": record["audio_url"],
                            "record": record if add_to_review else _normalize_record_for_ui(record),
                            "ai_eval": ai_eval_obj,
                            "ai_eval_debug": ai_debug_obj if bool(args.return_ai_debug) else None,
                        },
                    )
                except Exception as e:
                    return self._send_json(500, {"ok": False, "error": str(e)})

            if route == "/api/ai-eval-one":
                try:
                    if not openai_configured:
                        raise RuntimeError(
                            "OPENAI_API_KEY_SEEDLAB_ASR and OPENAI_API_KEY_SEEDLAB_JUDGE "
                            "(or OPENAI_FALLBACK_API_KEY / OPENAI_API_KEY) are required for /api/ai-eval-one"
                        )
                    body = self._read_json()
                    sample_id = str(body.get("sample_id") or "").strip()
                    if not sample_id:
                        raise RuntimeError("sample_id is required")
                    rec = self._resolve_record_by_sample_id(sample_id)
                    if not rec:
                        raise RuntimeError(f"record not found: {sample_id}")
                    sample_id_eval, eval_obj, debug_obj = _auto_eval_single_record(
                        run_dir=run_dir,
                        rec=rec,
                        asr_api_key=asr_api_key,
                        judge_api_key=judge_api_key,
                        asr_model=asr_model_resolved,
                        judge_model=judge_model,
                        language=str(args.language),
                        timeout_seconds=int(args.auto_eval_timeout),
                        evaluation_profile=str(args.evaluation_profile),
                        reference_audio_local_path=str(args.reference_audio_local_path),
                        reference_audio_s3_uri=str(args.reference_audio_s3_uri),
                        reference_audio_cache_dir=str(args.reference_audio_cache_dir),
                        disable_llm_note=bool(args.disable_llm_note),
                    )
                    with write_lock:
                        target_path = live_eval_path if sample_id.startswith("live:") else base_eval_path
                        _upsert_eval_entry(
                            target_path,
                            run_id=run_id,
                            sample_id=sample_id_eval,
                            eval_obj=eval_obj,
                            asr_model=asr_model_resolved,
                            judge_model=judge_model,
                        )
                        _append_jsonl_object(live_eval_debug_path, debug_obj)
                    return self._send_json(200, {"ok": True, "sample_id": sample_id_eval, "evaluation": eval_obj})
                except Exception as e:
                    return self._send_json(500, {"ok": False, "error": str(e)})

            if route == "/api/human-evals":
                try:
                    body = self._read_json()
                    evaluations = body.get("evaluations")
                    if not isinstance(evaluations, dict):
                        raise RuntimeError("evaluations must be an object")
                    normalized: dict[str, Any] = {}
                    for sample_id, eval_obj in evaluations.items():
                        if isinstance(eval_obj, dict):
                            normalized[str(sample_id)] = eval_obj
                    with write_lock:
                        _write_human_eval_map(human_eval_path, run_id=run_id, evaluations=normalized)
                    return self._send_json(200, {"ok": True, "run_id": run_id, "saved_count": len(normalized)})
                except Exception as e:
                    return self._send_json(500, {"ok": False, "error": str(e)})

            return self._send_json(404, {"ok": False, "error": f"unknown route: {route}"})

    server = SeedLabHttpServer((str(args.host), int(args.port)), SeedLabHandler)
    print("")
    print(f"[seed-lab] serve run_dir={run_dir}")
    print(f"[seed-lab] url=http://{args.host}:{int(args.port)}")
    print(f"[seed-lab] tts_endpoint={endpoint}")
    if asr_warning:
        print(f"[seed-lab] WARN: {asr_warning}")
    print(f"[seed-lab] asr_model={asr_model_resolved} (requested={asr_model_requested})")
    print(f"[seed-lab] judge_model={judge_model}")
    print(f"[seed-lab] evaluation_profile={args.evaluation_profile}")
    print(f"[seed-lab] auto_eval_on_add={auto_eval_on_add} openai_configured={openai_configured}")
    print("")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[seed-lab] serve stopped")
    finally:
        server.server_close()
    return 0


def _read_eval(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"eval file not found: {path}")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict) and isinstance(parsed.get("evaluations"), dict):
        return parsed["evaluations"]
    if isinstance(parsed, dict):
        return parsed
    raise RuntimeError("eval json must be object")


def _score_total(eval_obj: dict[str, Any]) -> float | None:
    if bool(eval_obj.get("rank_excluded")) or bool(eval_obj.get("prosody_fail")):
        return None
    try:
        weighted = float(eval_obj.get("weighted_ai_score_raw"))
        if 0 <= weighted <= 1:
            return 1.0 + weighted * 4.0
    except Exception:
        pass
    try:
        weighted_display = float(eval_obj.get("weighted_ai_score"))
        if 1 <= weighted_display <= 5:
            return weighted_display
    except Exception:
        pass
    vals = []
    for key in ("naturalness", "pronunciation", "stability", "tone_fit", "pitch_consistency", "artifact_cleanliness", "intonation_similarity"):
        try:
            num = float(eval_obj.get(key))
        except Exception:
            continue
        if 1 <= num <= 5:
            vals.append(num)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _build_seed_ranking(records: list[dict[str, Any]], eval_map: dict[str, Any]) -> list[dict[str, Any]]:
    seed_agg: dict[int, dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("status") not in ("ready", "skipped_existing"):
            continue
        sample_id = str(rec.get("sample_id") or "").strip()
        if not sample_id:
            continue
        seed = int(rec.get("seed") or 0)
        if seed <= 0:
            continue
        if seed not in seed_agg:
            seed_agg[seed] = {
                "seed": seed,
                "samples_total": 0,
                "samples_scored": 0,
                "score_sum": 0.0,
                "selected_count": 0,
                "notes": [],
                "script_ids": set(),
                "excluded": False,
                "exclude_reasons": [],
            }
        item = seed_agg[seed]
        item["samples_total"] += 1
        item["script_ids"].add(str(rec.get("script_id") or ""))
        ev = eval_map.get(sample_id)
        if isinstance(ev, dict):
            if bool(ev.get("rank_excluded")) or bool(ev.get("hard_artifact_fail")) or bool(ev.get("prosody_fail")):
                item["excluded"] = True
                reason = str(ev.get("hard_artifact_reason") or ev.get("prosody_fail_reason") or "excluded").strip()
                if reason and reason not in item["exclude_reasons"]:
                    item["exclude_reasons"].append(reason)
            total = _score_total(ev)
            if total is not None:
                item["samples_scored"] += 1
                item["score_sum"] += total
            if bool(ev.get("selected")):
                item["selected_count"] += 1
            note = str(ev.get("note") or "").strip()
            if note:
                item["notes"].append(note)

    ranking: list[dict[str, Any]] = []
    for seed, item in seed_agg.items():
        if item["excluded"]:
            continue
        samples_scored = int(item["samples_scored"])
        avg_score = (float(item["score_sum"]) / samples_scored) if samples_scored > 0 else 0.0
        ranking.append(
            {
                "seed": seed,
                "avg_score": round(avg_score, 4),
                "samples_total": int(item["samples_total"]),
                "samples_scored": samples_scored,
                "selected_count": int(item["selected_count"]),
                "script_ids": sorted(item["script_ids"]),
                "note_preview": " | ".join(item["notes"][:3])[:300],
            }
        )

    ranking.sort(
        key=lambda r: (
            -int(r["selected_count"]),
            -float(r["avg_score"]),
            -int(r["samples_scored"]),
            int(r["seed"]),
        )
    )
    return ranking


def _write_ranking_outputs(run_dir: Path, stem: str, ranking: list[dict[str, Any]], top_n: int) -> tuple[Path, Path]:
    top_rows = ranking[:top_n]
    report_json = run_dir / f"{stem}.json"
    report_json.write_text(json.dumps({"ranking": ranking, "top": top_rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    report_csv = run_dir / f"{stem}.csv"
    with report_csv.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=["seed", "avg_score", "samples_total", "samples_scored", "selected_count", "script_ids", "note_preview"],
        )
        writer.writeheader()
        for row in ranking:
            row_copy = dict(row)
            row_copy["script_ids"] = ",".join(row_copy["script_ids"])
            writer.writerow(row_copy)
    return report_csv, report_json


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("records")
    if not isinstance(records, list):
        raise RuntimeError("manifest.records missing")
    records = [r for r in records if isinstance(r, dict)]
    human_eval_map = _read_eval(Path(args.eval_json).resolve())
    human_ranking = _build_seed_ranking(records, human_eval_map)
    top_n = max(1, int(args.top))
    human_csv, human_json = _write_ranking_outputs(run_dir, "seed_ranking_human", human_ranking, top_n=top_n)

    # Backward compatibility aliases (human ranking)
    (run_dir / "seed_ranking.csv").write_text(human_csv.read_text(encoding="utf-8"), encoding="utf-8")
    (run_dir / "seed_ranking.json").write_text(human_json.read_text(encoding="utf-8"), encoding="utf-8")

    ai_eval_path = Path(args.ai_eval_json).resolve() if args.ai_eval_json else (run_dir / "auto_eval.json")
    ai_ranking: list[dict[str, Any]] = []
    ai_csv: Path | None = None
    ai_json: Path | None = None
    if ai_eval_path.exists():
        ai_eval_map = _read_eval(ai_eval_path)
        ai_ranking = _build_seed_ranking(records, ai_eval_map)
        ai_csv, ai_json = _write_ranking_outputs(run_dir, "seed_ranking_ai", ai_ranking, top_n=top_n)
        excluded_ai_seeds = sorted(
            {
                int(rec.get("seed") or 0)
                for rec in records
                if isinstance(rec, dict)
                and int(rec.get("seed") or 0) > 0
                and isinstance(ai_eval_map.get(str(rec.get("sample_id") or "").strip()), dict)
                and bool(ai_eval_map.get(str(rec.get("sample_id") or "").strip(), {}).get("rank_excluded"))
            }
        )
    else:
        excluded_ai_seeds = []

    stage_b_path = run_dir / "top_seeds_stage_b.txt"
    if args.prepare_stage_b:
        stage_b_seeds = [str(row["seed"]) for row in human_ranking[:top_n]]
        stage_b_path.write_text("\n".join(stage_b_seeds) + "\n", encoding="utf-8")

    env_path = run_dir / "env_snippet_top3.txt"
    top3 = [str(row["seed"]) for row in human_ranking[:3]]
    if len(top3) == 3:
        env_path.write_text(f"TTS_FIXED_SEEDS={','.join(top3)}\n", encoding="utf-8")
    else:
        env_path.write_text("# top3 seeds unavailable\n", encoding="utf-8")

    print("")
    print(f"[seed-lab] run_dir={run_dir}")
    print(f"[seed-lab] human_ranking_csv={human_csv}")
    print(f"[seed-lab] human_ranking_json={human_json}")
    if ai_csv and ai_json:
        print(f"[seed-lab] ai_ranking_csv={ai_csv}")
        print(f"[seed-lab] ai_ranking_json={ai_json}")
    if args.prepare_stage_b:
        print(f"[seed-lab] stage_b_seeds={stage_b_path}")
    print(f"[seed-lab] env_snippet={env_path}")
    print("")
    print("[top human seeds]")
    for idx, row in enumerate(human_ranking[:10], start=1):
        print(
            f"{idx:02d}. seed={row['seed']} avg={row['avg_score']:.2f} "
            f"scored={row['samples_scored']}/{row['samples_total']} selected={row['selected_count']}"
        )
    if ai_ranking:
        print("")
        print("[top ai seeds]")
        for idx, row in enumerate(ai_ranking[:10], start=1):
            print(
                f"{idx:02d}. seed={row['seed']} avg={row['avg_score']:.2f} "
                f"scored={row['samples_scored']}/{row['samples_total']} selected={row['selected_count']}"
            )
        if excluded_ai_seeds:
            print("")
            print("[excluded ai seeds]")
            print(", ".join(str(v) for v in excluded_ai_seeds[:50]))
    return 0


def sh_escape(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed Lab: random-seed TTS batch generation + local HTML evaluation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="generate audio batch and HTML review page")
    run_p.add_argument("--dataset", required=True, help="dataset path (.json preferred, .yaml requires PyYAML)")
    run_p.add_argument("--api-url", default="", help="RunPod TTS API base URL (or use TTS_API_URL)")
    run_p.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, help="run output root dir")
    run_p.add_argument("--run-id", default="", help="manual run id")
    run_p.add_argument("--stage", choices=["a", "b", "full"], default="a")
    run_p.add_argument("--script-ids", default="", help="override scripts by id csv (ex: s1,s2)")
    run_p.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="target seed count")
    run_p.add_argument("--takes-per-seed", type=int, default=DEFAULT_TAKES_PER_SEED, help="number of audios per seed")
    run_p.add_argument(
        "--seed-list",
        default="",
        help="explicit seed csv; if count < samples random seeds are auto-filled, if > samples truncated",
    )
    run_p.add_argument("--seeds-file", default="", help="explicit seeds file (.txt or .json)")
    run_p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run_p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run_p.add_argument("--retries", type=int, default=1)
    run_p.add_argument("--resume", action="store_true")
    run_p.set_defaults(func=cmd_run)

    rep_p = sub.add_parser("report", help="build ranking from exported eval JSON")
    rep_p.add_argument("--run-dir", required=True)
    rep_p.add_argument("--eval-json", required=True, help="exported evaluation JSON from review HTML")
    rep_p.add_argument("--ai-eval-json", default="", help="optional AI eval JSON (default: <run-dir>/auto_eval.json)")
    rep_p.add_argument("--top", type=int, default=DEFAULT_STAGE_B_TOP)
    rep_p.add_argument("--prepare-stage-b", action="store_true")
    rep_p.set_defaults(func=cmd_report)

    auto_p = sub.add_parser("auto-eval", help="ASR+LLM 기반 자동 평가 JSON 생성")
    auto_p.add_argument("--run-dir", required=True)
    auto_p.add_argument("--out-json", default="", help="output eval JSON path (default: <run-dir>/auto_eval.json)")
    auto_p.add_argument("--asr-model", default=DEFAULT_AUTO_EVAL_ASR_MODEL, help="OpenAI ASR model")
    auto_p.add_argument("--judge-model", default=DEFAULT_AUTO_EVAL_JUDGE_MODEL, help="OpenAI judge model")
    auto_p.add_argument("--evaluation-profile", default=DEFAULT_AUTO_EVAL_PROFILE, help="evaluation profile")
    auto_p.add_argument("--language", default="ko", help="ASR language hint")
    auto_p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    auto_p.add_argument("--timeout", type=int, default=DEFAULT_AUTO_EVAL_TIMEOUT)
    auto_p.add_argument("--openai-api-key", default="", help="optional shared key; fallback to OPENAI_FALLBACK_API_KEY/OPENAI_API_KEY")
    auto_p.add_argument("--openai-api-key-asr", default="", help="optional; fallback to OPENAI_API_KEY_SEEDLAB_ASR")
    auto_p.add_argument("--openai-api-key-judge", default="", help="optional; fallback to OPENAI_API_KEY_SEEDLAB_JUDGE")
    auto_p.add_argument("--reference-audio-local-path", default="", help="optional local/reference WAV path for tone similarity")
    auto_p.add_argument("--reference-audio-s3-uri", default="", help="optional S3 URI for reference WAV")
    auto_p.add_argument("--reference-audio-cache-dir", default="", help="optional cache dir for downloaded reference audio")
    auto_p.add_argument("--disable-llm-note", action="store_true")
    auto_p.add_argument("--top", type=int, default=DEFAULT_STAGE_B_TOP, help="only used in printed next-step command")
    auto_p.set_defaults(func=cmd_auto_eval)

    serve_p = sub.add_parser("serve", help="serve review UI + interactive TTS generation API")
    serve_p.add_argument("--run-dir", required=True)
    serve_p.add_argument("--api-url", default="", help="RunPod TTS API base URL (or use TTS_API_URL)")
    serve_p.add_argument("--host", default=DEFAULT_SERVE_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT)
    serve_p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="TTS generate timeout seconds")
    serve_p.add_argument("--disable-auto-eval-on-add", action="store_true")
    serve_p.add_argument("--openai-api-key", default="", help="optional shared key; fallback to OPENAI_FALLBACK_API_KEY/OPENAI_API_KEY")
    serve_p.add_argument("--openai-api-key-asr", default="", help="optional; fallback to OPENAI_API_KEY_SEEDLAB_ASR")
    serve_p.add_argument("--openai-api-key-judge", default="", help="optional; fallback to OPENAI_API_KEY_SEEDLAB_JUDGE")
    serve_p.add_argument("--asr-model", default=DEFAULT_AUTO_EVAL_ASR_MODEL)
    serve_p.add_argument("--judge-model", default=DEFAULT_AUTO_EVAL_JUDGE_MODEL)
    serve_p.add_argument("--evaluation-profile", default=DEFAULT_AUTO_EVAL_PROFILE)
    serve_p.add_argument("--reference-audio-local-path", default="", help="optional local/reference WAV path for tone similarity")
    serve_p.add_argument("--reference-audio-s3-uri", default="", help="optional S3 URI for reference WAV")
    serve_p.add_argument("--reference-audio-cache-dir", default="", help="optional cache dir for downloaded reference audio")
    serve_p.add_argument("--disable-llm-note", action="store_true")
    serve_p.add_argument("--language", default="ko")
    serve_p.add_argument("--auto-eval-timeout", type=int, default=DEFAULT_AUTO_EVAL_TIMEOUT)
    serve_p.add_argument("--return-ai-debug", action="store_true")
    serve_p.set_defaults(func=cmd_serve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as e:
        print(f"[seed-lab] ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
