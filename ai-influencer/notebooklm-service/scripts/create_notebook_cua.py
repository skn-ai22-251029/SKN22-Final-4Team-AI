"""NotebookLM 노트북 생성 CUA 스크립트.

Usage:
  python3 create_notebook_cua.py \
    --name "노마드코더 2025-03-18" \
    --channel-id "UCUpJs89fSBXNolQGOYKn0YQ" \
    --channel-name "노마드코더" \
    --output result.json \
    --headless

Output JSON:
  {"notebook_url": "https://notebooklm.google.com/..."}
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from generate_report_cua import (
    BROWSER_PROFILE_DIR,
    DATA_DIR,
    _launch_context_with_retry,
    _assert_allowed_url,
    _ensure_logged_in,
    _run_cua_loop,
    emit_cost_tracking_summary,
    set_cost_tracker_api_key_family,
)
from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("create_notebook_cua")

NOTEBOOKLM_HOME = "https://notebooklm.google.com"
LIBRARY_JSON = DATA_DIR / "library.json"


# ─────────────────────────────────────────
# library.json 토픽 관리
# ─────────────────────────────────────────

def _load_library() -> dict:
    if LIBRARY_JSON.exists():
        try:
            return json.loads(LIBRARY_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_library(lib: dict) -> None:
    LIBRARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_JSON.write_text(
        json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _register_notebook(
    notebook_url: str,
    notebook_name: str,
    channel_id: str,
    channel_name: str,
) -> None:
    """library.json의 channels[channel_id] 구조에 새 노트북 등록."""
    lib = _load_library()
    today = date.today().isoformat()

    ch = lib.setdefault("channels", {}).setdefault(channel_id, {"name": channel_name, "history": []})
    ch["name"] = channel_name
    ch["notebook_url"] = notebook_url
    ch.setdefault("history", []).insert(0, {"notebook_url": notebook_url, "date": today})
    ch["history"] = ch["history"][:30]

    _save_library(lib)
    logger.info("[library] 등록 완료: channel_id=%s → %s", channel_id, notebook_url)


# ─────────────────────────────────────────
# CUA 노트북 생성
# ─────────────────────────────────────────

def rename_notebook(page, client, notebook_name: str) -> bool:
    """CUA로 현재 열린 노트북 제목을 notebook_name으로 변경."""
    TASK = (
        "Task: Rename this NotebookLM notebook.\n"
        f"Target name: '{notebook_name}'\n"
        "Steps:\n"
        "1. Find the notebook title at the top of the page "
        "(currently 'Untitled notebook' or similar).\n"
        "2. Click on the title text to make it editable.\n"
        "3. Select all existing text (Ctrl+A) and delete it.\n"
        f"4. Type the new name: {notebook_name}\n"
        "5. Press Enter to confirm.\n"
        f'Output {{"action": "done"}} when the new title is visible.\n'
        "Do NOT navigate away from this page."
    )
    success = _run_cua_loop(page, client, TASK, max_steps=15, phase="RENAME_NB")
    if success:
        logger.info("[rename_notebook] 성공: %r", notebook_name)
    else:
        logger.warning("[rename_notebook] 실패 (15 스텝 초과) — 이름 미설정, URL은 유효")
    return success


def create_notebook(page, client, notebook_name: str) -> str:
    """CUA로 NotebookLM 홈에서 새 노트북을 생성하고 URL을 반환."""
    logger.info("[create_notebook] 홈 이동: %s", NOTEBOOKLM_HOME)
    page.goto(NOTEBOOKLM_HOME, wait_until="domcontentloaded", timeout=90000)
    _assert_allowed_url(page.url, "CREATE_NB_NAVIGATE")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    _ensure_logged_in(page, client)
    time.sleep(2)

    TASK = (
        "Task: Create a new notebook on NotebookLM.\n"
        f"Notebook name to set: '{notebook_name}'\n"
        "Steps:\n"
        "1. On the NotebookLM home page, find the '새 노트북' or '+ New notebook' or 'Create' button\n"
        "2. Click it\n"
        "3. If a name/title input dialog appears, clear any existing text and type the notebook name\n"
        f"   Name: {notebook_name}\n"
        "4. Confirm by pressing Enter or clicking the create/확인/OK button\n"
        "5. Wait for the new empty notebook page to fully load\n"
        f'Output {{"action": "done"}} when the new notebook page is open and the URL has changed to the notebook URL.\n'
        "Do NOT add any sources yet — just create the empty notebook."
    )

    if not _run_cua_loop(page, client, TASK, max_steps=12, phase="CREATE_NB"):
        raise RuntimeError("노트북 생성 실패 (12 스텝 초과)")

    time.sleep(2)
    notebook_url = page.url
    logger.info("[create_notebook] 생성 완료: %s", notebook_url)

    if "notebooklm.google.com/notebook/" not in notebook_url:
        raise RuntimeError(f"노트북 URL 획득 실패: {notebook_url}")

    return notebook_url


def _build_openai_client() -> OpenAI:
    api_key = (
        os.environ.get("OPENAI_API_KEY_CUA_CREATE_NOTEBOOK", "").strip()
        or os.environ.get("OPENAI_FALLBACK_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY_CUA_CREATE_NOTEBOOK 또는 OPENAI_FALLBACK_API_KEY "
            "(legacy OPENAI_API_KEY 포함)가 필요합니다."
        )
    return OpenAI(api_key=api_key)


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────

def main():
    set_cost_tracker_api_key_family("cua_create_notebook")
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="노트북 표시 이름 (예: '노마드코더 2025-03-18')")
    parser.add_argument("--channel-id", required=True, help="YouTube 채널 ID (예: 'UCUpJs89fSBXNolQGOYKn0YQ')")
    parser.add_argument("--channel-name", default="", help="채널 표시 이름 (예: '노마드코더')")
    parser.add_argument("--output", default="", help="결과 JSON 출력 경로")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    client = _build_openai_client()

    with sync_playwright() as p:
        context = _launch_context_with_retry(p, headless=args.headless, phase="CREATE_NOTEBOOK")
        page = context.new_page()

        notebook_url = create_notebook(page, client, args.name)
        # 이름 변경 (실패해도 notebook_url은 유효하므로 계속 진행)
        rename_notebook(page, client, args.name)
        context.close()

    _register_notebook(
        notebook_url=notebook_url,
        notebook_name=args.name,
        channel_id=args.channel_id,
        channel_name=args.channel_name,
    )

    result = {"notebook_url": notebook_url}

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False))
    print(f"✅ Created: channel_id={args.channel_id} → {notebook_url}")


if __name__ == "__main__":
    try:
        main()
    finally:
        emit_cost_tracking_summary()
