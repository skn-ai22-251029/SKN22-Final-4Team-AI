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
import json
import os
import random
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid
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
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px;
    }}
    .panel {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 14px;
      box-shadow: 0 4px 14px rgba(16, 42, 67, 0.06);
    }}
    .row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    input, select, button {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #fff;
      color: var(--text);
      font-size: 13px;
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
      font-size: 12px;
      table-layout: fixed;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 8px;
      vertical-align: top;
      text-align: left;
      word-wrap: break-word;
    }}
    th {{
      background: #f6f9fc;
      position: sticky;
      top: 0;
      z-index: 5;
    }}
    td audio {{
      width: 210px;
      height: 32px;
    }}
    .memo {{
      width: 100%;
      min-height: 54px;
    }}
    .muted {{
      color: var(--muted);
      font-size: 12px;
    }}
    .script-btn {{
      margin-top: 6px;
      padding: 6px 8px;
      font-size: 12px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #f6f9fc;
      color: var(--text);
      cursor: pointer;
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
        <span class="muted">저장 위치: localStorage (브라우저)</span>
      </div>
      <div class="row" style="margin-top:8px;">
        <span id="summary" class="muted"></span>
      </div>
    </div>
    <div class="panel" style="overflow:auto; max-height:70vh;">
      <table>
        <thead>
          <tr>
            <th style="width:74px;">seed</th>
            <th style="width:90px;">script_id</th>
            <th style="width:150px;">script</th>
            <th style="width:250px;">audio</th>
            <th style="width:56px;">자연</th>
            <th style="width:56px;">발음</th>
            <th style="width:56px;">안정</th>
            <th style="width:56px;">톤</th>
            <th style="width:62px;">평균</th>
            <th style="width:74px;">선택</th>
            <th>메모</th>
            <th style="width:90px;">status</th>
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
    const MANIFEST = {manifest_json};
    const SCORE_KEYS = ["naturalness", "pronunciation", "stability", "tone_fit"];

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

    function scoreAvg(evalObj) {{
      const vals = SCORE_KEYS.map(k => Number(evalObj[k])).filter(v => Number.isFinite(v) && v > 0);
      if (!vals.length) return "";
      return (vals.reduce((a,b) => a+b, 0) / vals.length).toFixed(2);
    }}

    function updateSummary(state) {{
      let total = 0;
      let visible = 0;
      let selectedCount = 0;
      let readyCount = 0;
      let scoredCount = 0;
      for (const rec of MANIFEST) {{
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
      for (const rec of MANIFEST) {{
        const sampleId = rec.sample_id;
        if (!state[sampleId]) state[sampleId] = emptyEval();
        const ev = state[sampleId];
        if (!rowVisible(rec, ev)) continue;
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
          audio.src = String(rec.audio_rel_path);
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

    const state = loadState();
    const scriptModal = document.getElementById("scriptModal");
    const scriptModalTitle = document.getElementById("scriptModalTitle");
    const scriptModalBody = document.getElementById("scriptModalBody");
    const scriptModalClose = document.getElementById("scriptModalClose");

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
    render(state);
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


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("records")
    if not isinstance(records, list):
        raise RuntimeError("manifest.records missing")
    eval_map = _read_eval(Path(args.eval_json).resolve())

    seed_agg: dict[int, dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("status") not in ("ready", "skipped_existing"):
            continue
        sample_id = str(rec.get("sample_id") or "").strip()
        seed = int(rec.get("seed"))
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

    top_n = max(1, int(args.top))
    top_rows = ranking[:top_n]

    report_json = run_dir / "seed_ranking.json"
    report_json.write_text(json.dumps({"ranking": ranking, "top": top_rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    report_csv = run_dir / "seed_ranking.csv"
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

    stage_b_path = run_dir / "top_seeds_stage_b.txt"
    if args.prepare_stage_b:
        stage_b_seeds = [str(row["seed"]) for row in top_rows]
        stage_b_path.write_text("\n".join(stage_b_seeds) + "\n", encoding="utf-8")

    env_path = run_dir / "env_snippet_top3.txt"
    top3 = [str(row["seed"]) for row in ranking[:3]]
    if len(top3) == 3:
        env_path.write_text(f"TTS_FIXED_SEEDS={','.join(top3)}\n", encoding="utf-8")
    else:
        env_path.write_text("# top3 seeds unavailable\n", encoding="utf-8")

    print("")
    print(f"[seed-lab] run_dir={run_dir}")
    print(f"[seed-lab] ranking_csv={report_csv}")
    print(f"[seed-lab] ranking_json={report_json}")
    if args.prepare_stage_b:
        print(f"[seed-lab] stage_b_seeds={stage_b_path}")
    print(f"[seed-lab] env_snippet={env_path}")
    print("")
    print("[top seeds]")
    for idx, row in enumerate(ranking[:10], start=1):
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
    rep_p.add_argument("--top", type=int, default=DEFAULT_STAGE_B_TOP)
    rep_p.add_argument("--prepare-stage-b", action="store_true")
    rep_p.set_defaults(func=cmd_report)

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
