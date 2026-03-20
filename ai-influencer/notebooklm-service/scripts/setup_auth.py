"""
AWS 서버에서 1회 실행 — Google 로그인 세션을 browser_profile에 저장.
Playwright remote-debugging-pipe 대신 바이너리 직접 실행 방식 사용.

사용법 (AWS):
    DISPLAY=:99 python3 scripts/setup_auth.py
"""
import os
import subprocess
from pathlib import Path

BROWSER_PROFILE_DIR = Path(__file__).parent.parent / "data" / "browser_state" / "browser_profile"

CHROME_CANDIDATES = [
    Path.home() / ".cache/ms-playwright/chromium-1155/chrome-linux/chrome",
    Path("/usr/bin/chromium-browser"),
    Path("/usr/bin/chromium"),
    Path("/usr/bin/google-chrome"),
]


def find_chrome() -> Path:
    for p in CHROME_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError("Chromium 바이너리를 찾을 수 없습니다.")


def main():
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"브라우저 프로파일 경로: {BROWSER_PROFILE_DIR}")

    chrome = find_chrome()
    print(f"Chromium 바이너리: {chrome}")

    env = os.environ.copy()
    env.setdefault("DISPLAY", ":99")

    proc = subprocess.Popen(
        [
            str(chrome),
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--user-data-dir={BROWSER_PROFILE_DIR}",
            "https://notebooklm.google.com",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"브라우저 시작됨 (PID: {proc.pid})")
    print("VNC에서 Google 로그인 → NotebookLM 접속까지 확인하세요.")
    print("완료 후 이 터미널에서 Enter를 누르면 브라우저가 종료됩니다...")
    input()

    proc.terminate()
    proc.wait()
    print("✅ 세션 저장 완료.")
    print(f"프로파일 위치: {BROWSER_PROFILE_DIR}")


if __name__ == "__main__":
    main()
