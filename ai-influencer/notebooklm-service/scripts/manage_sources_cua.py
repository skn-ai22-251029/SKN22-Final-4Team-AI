"""NotebookLM 소스 관리 CUA 스크립트.

Modes:
  list    → 현재 소스 목록 JSON 출력 (--output 경로)
  add     → 소스 URL 추가 (--source-url, --source-title)
  delete  → 소스 삭제 (--source-url 로 특정)
  cleanup → 한도 초과 소스를 오래된 순 삭제 (--max-sources, default=20)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from generate_report_cua import (
    BROWSER_PROFILE_DIR,
    _launch_context_with_retry,
    _ensure_logged_in,
    _assert_allowed_url,
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
logger = logging.getLogger("manage_sources_cua")

DATA_DIR = Path(__file__).parent.parent / "data"
SOURCES_LOG_PATH = DATA_DIR / "sources_log.json"
NOTEBOOKLM_HOME = "https://notebooklm.google.com"


def _build_openai_client() -> OpenAI:
    api_key = (
        os.environ.get("OPENAI_API_KEY_CUA_MANAGE_SOURCES", "").strip()
        or os.environ.get("OPENAI_FALLBACK_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY_CUA_MANAGE_SOURCES 또는 OPENAI_FALLBACK_API_KEY "
            "(legacy OPENAI_API_KEY 포함)가 필요합니다."
        )
    return OpenAI(api_key=api_key)


# ─────────────────────────────────────────
# 소스 로그 (sources_log.json)
# ─────────────────────────────────────────

def _load_log() -> dict:
    if SOURCES_LOG_PATH.exists():
        try:
            return json.loads(SOURCES_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"sources": []}


def _save_log(log: dict) -> None:
    SOURCES_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_LOG_PATH.write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _is_duplicate(url: str, notebook_url: str) -> bool:
    log = _load_log()
    return any(
        s["url"] == url and s.get("notebook_url") == notebook_url
        for s in log.get("sources", [])
    )


def _clean_notebook_url(notebook_url: str) -> str:
    parsed = urlparse(notebook_url)
    return urlunparse(parsed._replace(query="", fragment=""))


def _log_add(url: str, title: str, notebook_url: str) -> None:
    log = _load_log()
    log.setdefault("sources", []).append({
        "url": url,
        "title": title,
        "notebook_url": notebook_url,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_log(log)


def _log_delete(url: str, notebook_url: str) -> None:
    log = _load_log()
    log["sources"] = [
        s for s in log.get("sources", [])
        if not (s["url"] == url and s.get("notebook_url") == notebook_url)
    ]
    _save_log(log)


def _source_exists_on_page(page, notebook_url: str, source_url: str, source_title: str, client=None) -> bool:
    """실제 Sources 패널에 대상 소스가 보이는지 확인한다."""
    page.goto(_clean_notebook_url(notebook_url), wait_until="domcontentloaded", timeout=90000)
    _assert_allowed_url(page.url, "SRC_EXISTS_NAVIGATE")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    _ensure_logged_in(page, client)
    time.sleep(2)

    body_text = page.inner_text("body")
    source_pos = body_text.find("소스")
    if source_pos >= 0:
        body_text = body_text[source_pos:source_pos + 6000]

    candidates = []
    title = (source_title or "").strip()
    if title:
        candidates.append(title)
        if len(title) > 24:
            candidates.append(title[:24])
    if source_url:
        candidates.append(source_url)
        if "watch?v=" in source_url:
            candidates.append(source_url.split("watch?v=", 1)[1][:11])
        if "youtu.be/" in source_url:
            candidates.append(source_url.rsplit("/", 1)[-1][:11])

    for needle in candidates:
        if needle and needle in body_text:
            logger.info("[source_exists] UI 확인됨: %s", needle)
            return True

    return False


def _get_oldest_sources(notebook_url: str, keep: int) -> list[dict]:
    """notebook_url 기준으로 초과 소스 목록 반환 (오래된 것부터)."""
    log = _load_log()
    nb_sources = [
        s for s in log.get("sources", [])
        if s.get("notebook_url") == notebook_url
    ]
    nb_sources.sort(key=lambda x: x.get("added_at", ""))
    excess = max(0, len(nb_sources) - keep)
    return nb_sources[:excess]


# ─────────────────────────────────────────
# Playwright CUA 함수
# ─────────────────────────────────────────

def _try_dom_add_youtube(page, source_url: str) -> bool:
    """Playwright DOM으로 YouTube 소스 추가 시도. 성공 시 True."""
    try:
        # ?addSource=true 등 쿼리 파라미터가 붙은 경우 웹 검색 UI가 열릴 수 있음
        # 먼저 소스 패널의 "+ 소스 추가" 버튼 클릭
        add_btn = page.locator("button:has-text('소스 추가'), button:has-text('Add source')").first
        add_btn.wait_for(state="visible", timeout=8000)
        add_btn.click()
        time.sleep(1)

        # 소스 타입 선택 다이얼로그에서 "YouTube" 버튼 클릭
        yt_btn = page.locator(
            "button:has-text('YouTube'), [aria-label*='YouTube'], [data-source-type='youtube']"
        ).first
        yt_btn.wait_for(state="visible", timeout=8000)
        yt_btn.click()
        time.sleep(1)

        # URL 입력 필드에 YouTube URL 입력
        url_input = page.locator("input[type='url'], input[placeholder*='URL'], input[placeholder*='url']").first
        url_input.wait_for(state="visible", timeout=8000)
        url_input.fill(source_url)
        time.sleep(0.5)

        # 삽입/확인 버튼 클릭
        confirm_btn = page.locator(
            "button:has-text('삽입'), button:has-text('추가'), button:has-text('Insert'), button:has-text('Add')"
        ).last
        confirm_btn.wait_for(state="visible", timeout=5000)
        confirm_btn.click()

        logger.info("[dom_add_youtube] DOM 방식 성공")
        return True
    except Exception as e:
        logger.warning("[dom_add_youtube] DOM 방식 실패 → CUA 폴백: %s", e)
        return False


def add_source_cua(page, client, notebook_url: str, source_url: str, source_title: str) -> bool:
    """NotebookLM 소스 패널에 URL 추가. YouTube는 DOM 먼저, 실패 시 CUA 폴백."""
    # ?addSource=true 없는 깨끗한 URL로 이동
    clean_url = _clean_notebook_url(notebook_url)

    page.goto(clean_url, wait_until="domcontentloaded", timeout=90000)
    _assert_allowed_url(page.url, "ADD_SRC_NAVIGATE")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    _ensure_logged_in(page, client)
    time.sleep(2)

    is_youtube = "youtube.com/watch" in source_url or "youtu.be/" in source_url

    # YouTube는 DOM 방식 먼저 시도
    if is_youtube and _try_dom_add_youtube(page, source_url):
        time.sleep(3)
        success = True
    else:
        # CUA 폴백
        if is_youtube:
            TASK = (
                "Task: Add a YouTube video as a source to this NotebookLM notebook.\n"
                f"YouTube URL: {source_url}\n"
                "Steps:\n"
                "1. Find the Sources panel on the LEFT side of the screen.\n"
                "2. Click the '+ 소스 추가' (Add source) button.\n"
                "3. A source type panel appears. Find and click the 'YouTube' button.\n"
                "   The YouTube button has the YouTube logo/icon and the text 'YouTube'.\n"
                "   Do NOT click '웹사이트' or any other option.\n"
                "4. A URL input field appears. Type the URL:\n"
                f"   {source_url}\n"
                "5. Click the '삽입' or confirm button.\n"
                "6. Wait for the source to appear in the sources list.\n"
                f'Output {{"action": "done"}} when done.\n'
                "CRITICAL: Step 3 must be 'YouTube', not '웹사이트'."
            )
        else:
            TASK = (
                "Task: Add a web source to this NotebookLM notebook.\n"
                f"URL: {source_url}\n"
                "Steps:\n"
                "1. Click the '+ 소스 추가' button in the Sources panel.\n"
                "2. Select '웹사이트' or 'URL' option.\n"
                "3. Type the URL: {source_url}\n"
                "4. Click '삽입' or confirm.\n"
                f'Output {{"action": "done"}} when done.'
            )
        success = _run_cua_loop(page, client, TASK, max_steps=15, phase="ADD_SRC")
    if success:
        time.sleep(3)
        if _source_exists_on_page(page, notebook_url, source_url, source_title, client=client):
            logger.info("[add_source] 성공: %s", source_url)
            return True
        logger.error("[add_source] UI 검증 실패: %s", source_url)
        return False
    else:
        logger.error("[add_source] 실패 (15 스텝 초과): %s", source_url)
    return False


def delete_source_cua(page, client, notebook_url: str, source_url: str, source_title: str) -> bool:
    """CUA로 특정 소스(제목/URL로 식별)를 삭제."""
    page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
    _assert_allowed_url(page.url, "DEL_SRC_NAVIGATE")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    _ensure_logged_in(page, client)
    time.sleep(2)

    identify = source_title[:60] if source_title else source_url[:80]
    TASK = (
        "Task: Delete a specific source from the NotebookLM Sources panel (LEFT side).\n"
        f"Source to delete: '{identify}'\n"
        "Steps:\n"
        "1. Locate the source in the Sources list on the left side\n"
        "2. Hover over the source item to reveal its action menu\n"
        "3. Click the '...' (ellipsis/kebab) menu button for that source\n"
        "4. Select '삭제' or 'Remove' from the menu\n"
        "5. Confirm deletion if a dialog appears\n"
        f'Output {{"action": "done"}} when the source has been removed.\n'
        "If the source is not found, output done anyway."
    )
    success = _run_cua_loop(page, client, TASK, max_steps=12, phase="DEL_SRC")
    if success:
        logger.info("[delete_source] 성공: %s", identify)
    else:
        logger.error("[delete_source] 실패: %s", identify)
    return success


def list_sources_from_page(page, notebook_url: str, client=None) -> list[str]:
    """DOM에서 소스 패널 제목 목록 파싱 (GPT 불필요)."""
    page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
    _assert_allowed_url(page.url, "LIST_SRC_NAVIGATE")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    _ensure_logged_in(page, client)
    time.sleep(2)

    body_text = page.inner_text("body")

    # 소스 패널은 좌측. "소스" 섹션 이후 항목 파싱
    source_pos = body_text.find("소스")
    if source_pos < 0:
        return []

    section = body_text[source_pos:source_pos + 4000]
    lines = [l.strip() for l in section.split("\n") if l.strip()]

    excluded = {"소스", "소스 추가", "모든 소스", "소스 관리", "선택", "삭제", "취소", "+"}
    sources = []
    for line in lines[1:60]:
        if len(line) > 4 and line not in excluded and not line.startswith("http"):
            sources.append(line)
        if len(sources) >= 50:
            break

    logger.info("[list_sources] %d개 파싱됨", len(sources))
    return sources


def find_notebook_url_by_name_cua(page, client, channel_name: str) -> str:
    """NotebookLM 홈에서 channel_name으로 노트북을 찾아 URL 반환. 실패 시 '' 반환."""
    page.goto(NOTEBOOKLM_HOME, wait_until="domcontentloaded", timeout=90000)
    _assert_allowed_url(page.url, "FIND_NB_NAVIGATE")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    _ensure_logged_in(page, client)
    time.sleep(2)

    TASK = (
        "Task: Find and open a specific notebook on the NotebookLM home page.\n"
        f"Notebook to find: '{channel_name}'\n"
        "Steps:\n"
        "1. You are on the NotebookLM home page showing a grid of existing notebooks.\n"
        f"2. Find a notebook card whose title contains '{channel_name}'.\n"
        "   If multiple match, click the most recent one (highest date).\n"
        "3. Click that notebook card to open it.\n"
        "4. Wait for the URL to change to /notebook/...\n"
        f'Output {{"action": "done"}} when the notebook is open.\n'
        "If no matching notebook is found after scrolling, output done anyway."
    )
    success = _run_cua_loop(page, client, TASK, max_steps=15, phase="FIND_NB")
    if not success:
        logger.warning("[find_notebook] CUA 실패: channel_name=%r", channel_name)
        return ""

    time.sleep(2)
    url = page.url
    if "notebooklm.google.com/notebook/" not in url:
        logger.warning("[find_notebook] 노트북 URL 획득 실패: %s", url)
        return ""

    logger.info("[find_notebook] 발견: %r → %s", channel_name, url)
    return url


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────

def main():
    set_cost_tracker_api_key_family("cua_manage_sources")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["list", "add", "delete", "cleanup", "find"])
    parser.add_argument("--notebook-url", default="")
    parser.add_argument("--channel-name", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--source-title", default="")
    parser.add_argument("--max-sources", type=int, default=20)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    if args.mode != "find" and not args.notebook_url:
        parser.error("--notebook-url is required for this mode")

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    client = _build_openai_client()

    with sync_playwright() as p:
        context = _launch_context_with_retry(p, headless=args.headless, phase=f"MANAGE-{args.mode.upper()}")
        page = context.new_page()

        if args.mode == "find":
            if not args.channel_name:
                parser.error("--channel-name is required for --mode find")
            notebook_url = find_notebook_url_by_name_cua(page, client, args.channel_name)
            found = bool(notebook_url)
            result = {"notebook_url": notebook_url, "found": found}
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8"
                )
            print(json.dumps(result, ensure_ascii=False))
            context.close()
            if not found:
                sys.exit(1)
            return

        elif args.mode == "list":
            sources = list_sources_from_page(page, args.notebook_url, client=client)
            result = {"sources": sources, "count": len(sources)}
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8"
                )
            print(json.dumps(result, ensure_ascii=False))
            print(f"✅ {len(sources)} sources listed")

        elif args.mode == "add":
            if not args.source_url:
                parser.error("--source-url is required for --mode add")

            if _is_duplicate(args.source_url, args.notebook_url):
                logger.warning("[add] sources_log 중복 기록 제거 후 재시도: %s", args.source_url)
                _log_delete(args.source_url, args.notebook_url)

            success = add_source_cua(page, client, args.notebook_url, args.source_url, args.source_title)
            if success:
                _log_add(args.source_url, args.source_title or args.source_url, args.notebook_url)
                print(f"✅ Added: {args.source_url}")
            else:
                print(f"❌ Failed to add: {args.source_url}")
                sys.exit(1)

        elif args.mode == "delete":
            if not args.source_url:
                parser.error("--source-url is required for --mode delete")

            success = delete_source_cua(page, client, args.notebook_url, args.source_url, args.source_title)
            if success:
                _log_delete(args.source_url, args.notebook_url)
                print(f"✅ Deleted: {args.source_url}")
            else:
                print(f"❌ Failed to delete: {args.source_url}")
                sys.exit(1)

        elif args.mode == "cleanup":
            to_delete = _get_oldest_sources(args.notebook_url, keep=args.max_sources)
            if not to_delete:
                print(f"✅ No cleanup needed (≤{args.max_sources} sources)")
                context.close()
                return

            logger.info("[cleanup] %d개 소스 삭제 예정", len(to_delete))
            for src in to_delete:
                logger.info("[cleanup] 삭제: %s", src.get("title") or src["url"])
                success = delete_source_cua(
                    page, client, args.notebook_url,
                    src["url"], src.get("title", "")
                )
                if success:
                    _log_delete(src["url"], args.notebook_url)
                else:
                    logger.warning("[cleanup] 삭제 실패 — 계속 진행: %s", src["url"])
                time.sleep(2)

            print(f"✅ Cleanup done: {len(to_delete)} sources removed")

        context.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        emit_cost_tracking_summary()
