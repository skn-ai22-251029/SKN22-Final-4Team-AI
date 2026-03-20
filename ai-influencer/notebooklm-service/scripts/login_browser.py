"""
NotebookLM Google 로그인 세션 갱신 스크립트.

EC2에서 실행:
  docker exec -it ai-influencer-notebooklm-service-1 \
    bash -c "DISPLAY=:99 python3 /app/scripts/login_browser.py"

VNC 터널 (로컬 터미널에서):
  ssh -L 5900:localhost:5900 ubuntu@<EC2-IP>
  → VNC 클라이언트로 localhost:5900 접속
"""

import logging
import sys
import time

from playwright.sync_api import sync_playwright
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("login_browser")

BROWSER_PROFILE_DIR = Path(__file__).parent.parent / "data" / "browser_state" / "browser_profile"
TARGET_URL = "https://notebooklm.google.com"
WAIT_SECONDS = 300  # 5분 대기 (로그인 완료 후 Ctrl+C)


def main():
    logger.info("브라우저 프로필: %s", BROWSER_PROFILE_DIR)
    logger.info("DISPLAY=%s", __import__("os").environ.get("DISPLAY", "(없음)"))

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        logger.info("NotebookLM 접속 중...")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        logger.info("현재 URL: %s", page.url)

        if "accounts.google.com" in page.url or "signin" in page.url:
            logger.info("=== 로그인 페이지 감지 ===")
            logger.info("VNC 클라이언트에서 Google 계정으로 로그인하세요.")
            logger.info("로그인 완료 후 %d초 내에 세션이 자동 저장됩니다.", WAIT_SECONDS)
        else:
            logger.info("=== 이미 로그인됨 또는 메인 페이지 ===")
            logger.info("세션이 유효합니다. 창을 닫아도 됩니다.")

        logger.info("%d초 대기 중 (Ctrl+C로 종료 가능)...", WAIT_SECONDS)
        try:
            for remaining in range(WAIT_SECONDS, 0, -10):
                time.sleep(10)
                current_url = page.url
                if "notebooklm.google.com" in current_url:
                    logger.info("✅ NotebookLM 접속 확인 — 세션 저장 완료. 남은 대기: %ds", remaining)
        except KeyboardInterrupt:
            logger.info("수동 종료")

        context.close()
        logger.info("브라우저 종료 — 세션이 프로필에 저장되었습니다.")


if __name__ == "__main__":
    main()
