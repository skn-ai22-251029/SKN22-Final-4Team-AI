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
import concurrent.futures
import csv
import datetime as dt
import html
import http.server
import json
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
LIVE_RECORDS_JSONL = "live_records.jsonl"
LIVE_AUTO_EVAL_JSON = "auto_eval_live.json"


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
    payload["mode"] = str(payload.get("mode") or "auto_eval_asr_llm")
    payload["asr_model"] = str(payload.get("asr_model") or asr_model)
    payload["judge_model"] = str(payload.get("judge_model") or judge_model)
    evaluations = payload.get("evaluations")
    if not isinstance(evaluations, dict):
        evaluations = {}
        payload["evaluations"] = evaluations
    evaluations[sample_id] = eval_obj
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
    const MANIFEST = {manifest_json};
    const SCORE_KEYS = ["naturalness", "pronunciation", "stability", "tone_fit"];
    let records = [...MANIFEST];
    let serverConfig = {{
      openai_configured: false,
      auto_eval_on_add: true,
    }};
    let serverConfigLoaded = false;
    let humanSort = {{ key: "", dir: "asc" }};

    function emptyEval() {{
      return {{
        naturalness: "",
        pronunciation: "",
        stability: "",
        tone_fit: "",
        note: "",
        selected: false,
        updated_at: "",
      }};
    }}

    function nowIso() {{
      return new Date().toISOString();
    }}

    function loadState() {{
      try {{
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return {{}};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {{}};
      }} catch (_e) {{
        return {{}};
      }}
    }}

    function saveState(state) {{
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

    async function apiGet(path) {{
      const resp = await fetch(path, {{ method: "GET" }});
      if (!resp.ok) {{
        const text = await resp.text();
        throw new Error(`HTTP ${{resp.status}}: ${{text.slice(0, 200)}}`);
      }}
      return resp.json();
    }}

    async function apiPost(path, body) {{
      const resp = await fetch(path, {{
        method: "POST",
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

    function scoreAvg(evalObj) {{
      const vals = SCORE_KEYS.map(k => Number(evalObj[k])).filter(v => Number.isFinite(v) && v > 0);
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
          const v = Number(scoreAvg(ai));
          return Number.isFinite(v) ? v : -1;
        }}
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
        tdAiAvg.textContent = scoreAvg(ai) || "-";
        tr.appendChild(tdAiAvg);

        const tdAiNote = document.createElement("td");
        tdAiNote.textContent = String(ai.note || "-");
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
      updateSortIndicators();
    }}

    function updateAiSummary(aiMap) {{
      const total = Object.keys(aiMap).length;
      let ready = 0;
      let failed = 0;
      for (const value of Object.values(aiMap)) {{
        if (!value || typeof value !== "object") continue;
        if (String(value.auto_eval_status || "ready") === "ready") ready += 1;
        else failed += 1;
      }}
      document.getElementById("aiSummary").textContent =
        `rows: ${{total}} / ready: ${{ready}} / failed: ${{failed}}`;
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
        aiHint.textContent = "OPENAI_API_KEY 미설정(또는 quickstart env 미전달): 자동 AI 평가는 비활성 상태입니다.";
        return;
      }}
      if (!serverConfig.auto_eval_on_add) {{
        aiHint.textContent = "자동 AI 평가가 비활성화되어 있습니다. (--disable-auto-eval-on-add)";
        return;
      }}
      aiHint.textContent = "아직 AI 평가 데이터가 없습니다. '평가 테이블에 추가' 체크 후 TTS 생성하거나 auto-eval을 실행하세요.";
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
        mode: "auto_eval_asr_llm",
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
        const payload = await apiGet("/api/live-records");
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
        const payload = await apiGet("/api/ai-evals");
        const incoming = payload && typeof payload === "object" ? payload.evaluations : {{}};
        aiState = incoming && typeof incoming === "object" ? incoming : {{}};
        saveAiState(aiState);
      }} catch (_e) {{
        aiState = loadAiState();
      }}
      if (renderAfter) render(state);
    }}

    async function loadServerConfig() {{
      try {{
        const payload = await apiGet("/api/config");
        serverConfigLoaded = true;
        serverConfig = {{
          openai_configured: !!(payload && payload.openai_configured),
          auto_eval_on_add: payload && payload.auto_eval_on_add !== false,
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
        }};
        // file:// 또는 API 미기동 상태에서는 무시
      }}
    }}

    const state = loadState();
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
        const out = await apiPost("/api/tts/generate", payload);
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
    loadServerConfig().then(() => refreshLiveRecords(false).then(() => refreshAiState(true)));
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


def _openai_api_key(explicit_key: str) -> str:
    key = (explicit_key or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for auto-eval")
    return key


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
) -> str:
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
        return text
    raise RuntimeError("empty transcript")


def _openai_judge_scores(
    *,
    api_key: str,
    judge_model: str,
    timeout_seconds: int,
    script_text: str,
    transcript_text: str,
    char_accuracy: float,
    length_ratio: float,
    chars_per_sec: float,
) -> dict[str, Any]:
    system_prompt = (
        "당신은 한국어 TTS 품질 평가자다. "
        "반드시 JSON 객체만 반환한다. "
        "점수는 1~5 정수로만 준다. "
        "키는 naturalness, pronunciation, stability, tone_fit, note 이다."
    )
    user_prompt = (
        "아래 정보를 보고 TTS 품질을 평가하라.\n"
        f"- 기준 대본:\n{script_text}\n\n"
        f"- ASR 전사:\n{transcript_text}\n\n"
        f"- 자동 지표: char_accuracy={char_accuracy:.4f}, length_ratio={length_ratio:.4f}, chars_per_sec={chars_per_sec:.4f}\n\n"
        "평가 기준:\n"
        "1) naturalness: 듣기 자연스러움\n"
        "2) pronunciation: 발음/전사 일치도\n"
        "3) stability: 흔들림/깨짐/일관성\n"
        "4) tone_fit: 의도한 화자 톤 적합성\n"
        "5) note: 핵심 판단 근거를 한국어 1~2문장으로 작성\n"
        "주의:\n"
        "- 3~4점 남발 금지. 불확실하면 보수적으로 낮게 준다.\n"
        "- 근거 없이 5점을 주지 않는다.\n"
        "- 지표가 나쁘면(낮은 정확도/길이 비정상/말하기 속도 비정상) 반드시 낮은 점수를 준다.\n"
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
    return parsed


def _auto_eval_single_record(
    *,
    run_dir: Path,
    rec: dict[str, Any],
    api_key: str,
    asr_model: str,
    judge_model: str,
    language: str,
    timeout_seconds: int,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    sample_id = str(rec.get("sample_id") or "").strip()
    seed = int(rec.get("seed") or 0)
    script_id = str(rec.get("script_id") or "")
    script_text = str(rec.get("script_text") or "")
    audio_rel_path = str(rec.get("audio_rel_path") or "").strip()
    audio_path = (run_dir / audio_rel_path).resolve()
    started = time.time()

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

    try:
        transcript_text = _openai_transcribe_audio(
            api_key=api_key,
            audio_path=audio_path,
            asr_model=asr_model,
            language=language,
            timeout_seconds=timeout_seconds,
        )
        ref_norm = _normalize_compare_text(script_text)
        hyp_norm = _normalize_compare_text(transcript_text)
        char_acc = _char_accuracy(ref_norm, hyp_norm)
        length_ratio = (len(hyp_norm) / len(ref_norm)) if ref_norm else 0.0
        duration = _wav_duration_seconds(audio_path)
        chars_per_sec = (len(hyp_norm) / duration) if duration > 0 else 0.0

        judged = _openai_judge_scores(
            api_key=api_key,
            judge_model=judge_model,
            timeout_seconds=timeout_seconds,
            script_text=script_text,
            transcript_text=transcript_text,
            char_accuracy=char_acc,
            length_ratio=length_ratio,
            chars_per_sec=chars_per_sec,
        )

        naturalness = _coerce_score(judged.get("naturalness"))
        pronunciation = _coerce_score(judged.get("pronunciation"))
        stability = _coerce_score(judged.get("stability"))
        tone_fit = _coerce_score(judged.get("tone_fit"))

        if char_acc < 0.70:
            pronunciation = _cap_score(pronunciation, 1)
        elif char_acc < 0.82:
            pronunciation = _cap_score(pronunciation, 2)
        elif char_acc < 0.92:
            pronunciation = _cap_score(pronunciation, 3)
        if length_ratio < 0.75 or length_ratio > 1.30:
            stability = _cap_score(stability, 2)
        elif length_ratio < 0.85 or length_ratio > 1.15:
            stability = _cap_score(stability, 3)
        if chars_per_sec < 2.3 or chars_per_sec > 11.5:
            naturalness = _cap_score(naturalness, 2)
        elif chars_per_sec < 3.0 or chars_per_sec > 10.0:
            naturalness = _cap_score(naturalness, 3)

        raw_note = str(judged.get("note") or "").strip()
        metrics_note = f"acc={char_acc:.3f}, len={length_ratio:.2f}, cps={chars_per_sec:.2f}"
        note = f"[AI] {metrics_note} | {raw_note}".strip()

        eval_obj = {
            "naturalness": naturalness,
            "pronunciation": pronunciation,
            "stability": stability,
            "tone_fit": tone_fit,
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
            "asr_model": asr_model,
            "judge_model": judge_model,
            "transcript_text": transcript_text,
            "char_accuracy": round(char_acc, 6),
            "length_ratio": round(length_ratio, 6),
            "chars_per_sec": round(chars_per_sec, 6),
            "duration_sec": round(duration, 6),
            "judge_raw": judged,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        return sample_id, eval_obj, debug
    except Exception as e:
        msg = str(e).strip() or "auto eval failed"
        eval_obj = _empty_eval(f"AUTO-EVAL FAILED: {msg}", status="failed")
        debug = {
            "sample_id": sample_id,
            "seed": seed,
            "script_id": script_id,
            "status": "failed",
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

    api_key = _openai_api_key(args.openai_api_key)
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
                    api_key=api_key,
                    asr_model=asr_model_resolved,
                    judge_model=judge_model,
                    language=str(args.language),
                    timeout_seconds=int(args.timeout),
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
        "mode": "auto_eval_asr_llm",
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

    openai_api_key = (args.openai_api_key or os.getenv("OPENAI_API_KEY") or "").strip()
    asr_model_requested = str(args.asr_model)
    asr_model_resolved, asr_warning = _resolve_asr_model_for_transcription(asr_model_requested)
    judge_model = str(args.judge_model)
    auto_eval_on_add = not bool(args.disable_auto_eval_on_add)
    live_records_path = run_dir / LIVE_RECORDS_JSONL
    live_eval_path = run_dir / LIVE_AUTO_EVAL_JSON
    live_eval_debug_path = run_dir / "auto_eval_live_debug.jsonl"
    base_eval_path = run_dir / "auto_eval.json"
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

            if route == "/api/health":
                return self._send_json(
                    200,
                    {
                        "ok": True,
                        "run_id": run_id,
                        "tts_endpoint": endpoint,
                        "auto_eval_on_add": auto_eval_on_add,
                        "openai_configured": bool(openai_api_key),
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
                        "openai_configured": bool(openai_api_key),
                        "asr_model": asr_model_resolved,
                        "asr_model_requested": asr_model_requested,
                        "judge_model": judge_model,
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
                    }

                    add_to_review = _to_bool(body.get("add_to_review"), default=False)
                    ai_eval_obj: dict[str, Any] | None = None
                    ai_debug_obj: dict[str, Any] | None = None

                    if add_to_review:
                        with write_lock:
                            _append_jsonl_object(live_records_path, record)

                        if auto_eval_on_add and openai_api_key:
                            sample_id_eval, eval_obj, debug_obj = _auto_eval_single_record(
                                run_dir=run_dir,
                                rec=record,
                                api_key=openai_api_key,
                                asr_model=asr_model_resolved,
                                judge_model=judge_model,
                                language=str(args.language),
                                timeout_seconds=int(args.auto_eval_timeout),
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
                    if not openai_api_key:
                        raise RuntimeError("OPENAI_API_KEY is required for /api/ai-eval-one")
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
                        api_key=openai_api_key,
                        asr_model=asr_model_resolved,
                        judge_model=judge_model,
                        language=str(args.language),
                        timeout_seconds=int(args.auto_eval_timeout),
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
    print(f"[seed-lab] auto_eval_on_add={auto_eval_on_add} openai_configured={bool(openai_api_key)}")
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
    vals = []
    for key in ("naturalness", "pronunciation", "stability", "tone_fit"):
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
            }
        item = seed_agg[seed]
        item["samples_total"] += 1
        item["script_ids"].add(str(rec.get("script_id") or ""))
        ev = eval_map.get(sample_id)
        if isinstance(ev, dict):
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
    auto_p.add_argument("--language", default="ko", help="ASR language hint")
    auto_p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    auto_p.add_argument("--timeout", type=int, default=DEFAULT_AUTO_EVAL_TIMEOUT)
    auto_p.add_argument("--openai-api-key", default="", help="optional; fallback to OPENAI_API_KEY")
    auto_p.add_argument("--top", type=int, default=DEFAULT_STAGE_B_TOP, help="only used in printed next-step command")
    auto_p.set_defaults(func=cmd_auto_eval)

    serve_p = sub.add_parser("serve", help="serve review UI + interactive TTS generation API")
    serve_p.add_argument("--run-dir", required=True)
    serve_p.add_argument("--api-url", default="", help="RunPod TTS API base URL (or use TTS_API_URL)")
    serve_p.add_argument("--host", default=DEFAULT_SERVE_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT)
    serve_p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="TTS generate timeout seconds")
    serve_p.add_argument("--disable-auto-eval-on-add", action="store_true")
    serve_p.add_argument("--openai-api-key", default="", help="optional; fallback to OPENAI_API_KEY")
    serve_p.add_argument("--asr-model", default=DEFAULT_AUTO_EVAL_ASR_MODEL)
    serve_p.add_argument("--judge-model", default=DEFAULT_AUTO_EVAL_JUDGE_MODEL)
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
