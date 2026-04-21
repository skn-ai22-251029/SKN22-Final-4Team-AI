import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page
from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("generate_report_cua")

DATA_DIR = Path(__file__).parent.parent / "data"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_state" / "browser_profile"
STORAGE_STATE_PATH = DATA_DIR / "browser_state" / "storage_state.json"
CHROMIUM_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
_BROWSER_INSTALL_ATTEMPTED = False
_COST_TRACKING_MARKER = "COST_TRACKING_JSON:"
_COST_TRACKER_API_KEY_FAMILY = "cua_generate_report"
_COST_TRACKER: dict[str, object] = {
    "api_key_family": _COST_TRACKER_API_KEY_FAMILY,
    "request_count": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "models": {},
}

SYSTEM_PROMPT = """You are a browser automation assistant controlling a Chromium browser via Playwright.
Given a screenshot of the current browser state and a task, output a JSON action to perform.

Output ONLY valid JSON in one of these formats:

1. Click: {"action": "click", "x": <int>, "y": <int>, "reason": "<why>"}
2. Type: {"action": "type", "text": "<text to type>"}
3. Key: {"action": "key", "key": "<key name e.g. Enter, Tab>"}
4. Scroll: {"action": "scroll", "x": <int>, "y": <int>, "delta_y": <int>}
5. Wait: {"action": "wait", "ms": <milliseconds>}
6. Done: {"action": "done"}

Rules:
- Output ONLY the JSON object, no explanation.
- Use "done" only when the report is fully generated and visible on screen.
- Do NOT include report text in the done action — text will be extracted from the DOM automatically.
- If the report is still generating, use "wait".
- Coordinates must be within 1280x800 viewport.
"""

LIST_SYSTEM_PROMPT = """You are a browser automation assistant for listing saved report titles in NotebookLM Studio.
Given a screenshot and task context, output ONLY one JSON object in one of these formats:

1) Collect visible titles:
{"action":"collect","titles":["title1","title2",...]}

2) Click:
{"action":"click","x":<int>,"y":<int>,"reason":"<why>"}

3) Scroll:
{"action":"scroll","x":<int>,"y":<int>,"delta_y":<int>}

4) Wait:
{"action":"wait","ms":<milliseconds>}

5) Done:
{"action":"done"}

Rules:
- `titles` must contain only titles actually visible in the screenshot.
- Do not fabricate titles.
- Do not click a report tile in list mode; only navigate/open Studio panel if needed.
- Prefer `collect` when titles are visible, `scroll` when more titles may exist.
- Output only JSON and nothing else.
"""


def reset_cost_tracker() -> None:
    global _COST_TRACKER
    _COST_TRACKER = {
        "api_key_family": _COST_TRACKER_API_KEY_FAMILY,
        "request_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "models": {},
    }


def set_cost_tracker_api_key_family(api_key_family: str) -> None:
    global _COST_TRACKER_API_KEY_FAMILY
    _COST_TRACKER_API_KEY_FAMILY = str(api_key_family or "").strip() or "cua_generate_report"
    reset_cost_tracker()


def _record_openai_usage(response: object, default_model: str) -> None:
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
        return
    model_name = str(getattr(response, "model", "") or default_model or "")
    _COST_TRACKER["request_count"] = int(_COST_TRACKER.get("request_count") or 0) + 1
    _COST_TRACKER["prompt_tokens"] = int(_COST_TRACKER.get("prompt_tokens") or 0) + prompt_tokens
    _COST_TRACKER["completion_tokens"] = int(_COST_TRACKER.get("completion_tokens") or 0) + completion_tokens
    _COST_TRACKER["total_tokens"] = int(_COST_TRACKER.get("total_tokens") or 0) + total_tokens
    models = _COST_TRACKER.get("models") if isinstance(_COST_TRACKER.get("models"), dict) else {}
    model_bucket = models.setdefault(model_name, {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    model_bucket["request_count"] = int(model_bucket.get("request_count") or 0) + 1
    model_bucket["prompt_tokens"] = int(model_bucket.get("prompt_tokens") or 0) + prompt_tokens
    model_bucket["completion_tokens"] = int(model_bucket.get("completion_tokens") or 0) + completion_tokens
    model_bucket["total_tokens"] = int(model_bucket.get("total_tokens") or 0) + total_tokens
    _COST_TRACKER["models"] = models


def emit_cost_tracking_summary() -> None:
    print(f"{_COST_TRACKING_MARKER}{json.dumps(_COST_TRACKER, ensure_ascii=False)}")

LOGIN_SYSTEM_PROMPT = """You are a browser automation assistant controlling a Chromium browser via Playwright for Google sign-in.
Given a screenshot of the current browser state and a task, output a JSON action to perform.

Output ONLY valid JSON in one of these formats:

1. Click: {"action": "click", "x": <int>, "y": <int>, "reason": "<why>"}
2. Key: {"action": "key", "key": "<key name e.g. Enter, Tab>"}
3. Scroll: {"action": "scroll", "x": <int>, "y": <int>, "delta_y": <int>}
4. Wait: {"action": "wait", "ms": <milliseconds>}
5. Done: {"action": "done"}

Rules:
- Output ONLY the JSON object, no explanation.
- Never type credentials yourself.
- Use "done" only when the requested login milestone is clearly reached on screen.
- You may click Google sign-in controls such as Next, Continue, Use another account, Try again, Skip, Not now.
- Stay within visible Google/NotebookLM controls only.
"""

ALLOWED_CUA_HOSTS = {
    "notebooklm.google.com",
    "accounts.google.com",
}

# NotebookLM 보고서 컨테이너 후보 셀렉터 (우선순위 순)
_REPORT_SELECTORS = [
    "ms-content-chunk",
    "[class*='output-text']",
    "[class*='StudioOutput']",
    "[class*='studio-output']",
    "[class*='generated-output']",
    "[class*='OutputContent']",
    ".ProseMirror",
    "[contenteditable='true']",
    "[class*='report-content']",
]


def _is_notebooklm_ready_url(url: str) -> bool:
    return "notebooklm.google.com" in (url or "") and "accounts.google.com" not in (url or "")


def _is_visible(page: Page, selector: str, timeout: int = 1200) -> bool:
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def _page_contains_text(page: Page, needles: list[str]) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=1500)
    except Exception:
        return False
    lowered = body.lower()
    return any(needle.lower() in lowered for needle in needles)


def _get_login_state(page: Page) -> str:
    if _is_notebooklm_ready_url(page.url):
        return "logged_in"
    if "signin/challenge/pwd" in (page.url or "") or "/challenge/pwd" in (page.url or ""):
        return "password"
    if _page_contains_text(page, ["패스키", "passkey", "다른 방법 선택", "try another way", "비밀번호 입력", "enter your password"]):
        return "passkey"
    if _is_visible(page, "input[type='password']"):
        return "password"
    if _is_visible(page, "input[type='email']"):
        return "email"
    return "unknown"


def _run_login_cua_step(page: Page, client: OpenAI, task: str, phase: str, max_steps: int = 12) -> None:
    success = _run_cua_loop(
        page,
        client,
        task,
        max_steps=max_steps,
        phase=phase,
        allowed_actions={"click", "key", "scroll", "wait", "done"},
        system_prompt=LOGIN_SYSTEM_PROMPT,
    )
    if not success:
        raise RuntimeError(f"Google 로그인 CUA 실패 ({phase}, {max_steps} 스텝 초과)")


def _ensure_logged_in(page: Page, client: Optional[OpenAI] = None) -> None:
    """Google 로그인이 필요한 경우 자동으로 로그인한다.
    GOOGLE_EMAIL / GOOGLE_PASSWORD 환경변수가 설정된 경우에만 동작."""
    email = os.environ.get("GOOGLE_EMAIL", "")
    password = os.environ.get("GOOGLE_PASSWORD", "")

    if not email or not password:
        logger.info("[login] GOOGLE_EMAIL/PASSWORD 미설정 — 자동 로그인 건너뜀")
        return

    # 로그인 페이지 여부 확인
    if "accounts.google.com" not in page.url and "signin" not in page.url:
        logger.info("[login] 이미 로그인됨 (url=%s)", page.url)
        return

    logger.info("[login] 로그인 페이지 감지 — 자동 로그인 시작")
    client = client or _build_openai_client()

    try:
        state = _get_login_state(page)
        if state not in {"email", "password", "logged_in"}:
            _run_login_cua_step(
                page,
                client,
                (
                    "Task: Navigate the current Google sign-in flow until one of these is true:\n"
                    "1. the email input is visible and focused, or\n"
                    "2. the password input is visible and focused, or\n"
                    "3. the NotebookLM page is open.\n"
                    "If account chooser, rejected, or interstitial pages appear, use controls like "
                    "'Use another account', '다른 계정 사용', 'Try again', 'Continue', 'Next', '다음' "
                    "to return to the normal sign-in flow.\n"
                    'Output {"action":"done"} only when one of the three milestones above is reached.\n'
                    "Never type any credentials."
                ),
                phase="LOGIN_PREPARE",
                max_steps=16,
            )

        state = _get_login_state(page)
        if state == "logged_in":
            logger.info("[login] 이미 NotebookLM 진입 완료 — url=%s", page.url)
            return

        if state == "email":
            for attempt in range(3):
                page.locator("input[type='email']").first.fill(email)
                logger.info("[login] 이메일 입력 완료 (attempt=%d/3)", attempt + 1)
                _run_login_cua_step(
                    page,
                    client,
                    (
                        "Task: The Google email address has already been typed into the email field.\n"
                        "Continue the sign-in flow until one of these is true:\n"
                        "1. the password input is visible and focused,\n"
                        "2. a passkey / '다른 방법 선택' / '비밀번호 입력' screen is visible, or\n"
                        "3. the NotebookLM page is open.\n"
                        "Important passkey handling:\n"
                        "- If a passkey screen appears, click '취소' or 'Cancel' first.\n"
                        "- Then click '다른 방법 선택' or 'Try another way'.\n"
                        "- Then click '비밀번호 입력' or 'Enter your password'.\n"
                        "- If a rejected or interstitial page appears, use 'Try again', 'Continue', 'Next', or "
                        "'Use another account' only when needed to return to the normal sign-in flow.\n"
                        'Output {"action":"done"} only when one of the three milestones above is reached.\n'
                        "Never type any credentials."
                    ),
                    phase="LOGIN_EMAIL_NEXT",
                    max_steps=12,
                )
                state = _get_login_state(page)
                if state in {"password", "passkey", "logged_in"}:
                    break
                logger.warning("[login] 이메일 단계 후 다시 email 상태로 복귀 — 이메일 재주입 후 재시도")

        state = _get_login_state(page)
        if state == "logged_in":
            logger.info("[login] 이메일 단계 후 NotebookLM 진입 완료 — url=%s", page.url)
            return
        if state != "password":
            _run_login_cua_step(
                page,
                client,
                (
                    "Task: Reach the Google password input field.\n"
                    "Important passkey handling:\n"
                    "1. If a passkey screen appears, click '취소' or 'Cancel'.\n"
                    "2. Click '다른 방법 선택' or 'Try another way'.\n"
                    "3. Click '비밀번호 입력' or 'Enter your password'.\n"
                    "4. If you are on a rejected/interstitial page, use 'Try again', 'Continue', 'Next', or "
                    "'Use another account' only to get back into the sign-in flow.\n"
                    'Output {"action":"done"} only when the password input field is visible and focused.\n'
                    "Never type any credentials."
                ),
                phase="LOGIN_PASSWORD_PREPARE",
                max_steps=24,
            )

        state = _get_login_state(page)
        if state != "password":
            raise RuntimeError(f"비밀번호 입력 화면 도달 실패 (state={state}, url={page.url})")

        page.wait_for_selector("input[type='password']", timeout=15000)
        time.sleep(0.5)
        page.locator("input[type='password']").first.fill(password)
        logger.info("[login] 비밀번호 입력 완료")
        _run_login_cua_step(
            page,
            client,
            (
                "Task: The Google password has already been typed into the password field.\n"
                "Complete sign-in and continue through any post-password Google prompts until NotebookLM is open.\n"
                "If additional prompts appear, prefer the safest option that continues sign-in without extra setup, "
                "such as Next, Continue, Skip, or Not now.\n"
                'Output {"action":"done"} only when the NotebookLM page is open.\n'
                "Never type any credentials."
            ),
            phase="LOGIN_PASSWORD_SUBMIT",
            max_steps=20,
        )

        page.wait_for_url("**/notebooklm.google.com/**", timeout=30000)
        logger.info("[login] 로그인 성공 — url=%s", page.url)

    except Exception as e:
        logger.error("[login] 자동 로그인 실패: %s", e)
        logger.error("[login] 현재 URL: %s", page.url)
        raise RuntimeError(f"Google 자동 로그인 실패: {e}")


def _assert_allowed_url(current_url: str, phase: str) -> None:
    """CUA가 허용된 도메인에서만 동작하도록 강제."""
    from urllib.parse import urlparse

    host = (urlparse(current_url).hostname or "").lower()
    if host and host not in ALLOWED_CUA_HOSTS:
        raise RuntimeError(f"[CUA][{phase}] 허용되지 않은 도메인 접근 차단: {host}")


def _build_openai_client() -> OpenAI:
    """CUA는 전용 키 우선 사용. 없으면 일반 키를 fallback."""
    api_key = (
        os.environ.get("OPENAI_API_KEY_CUA_GENERATE_REPORT", "").strip()
        or os.environ.get("OPENAI_FALLBACK_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY_CUA_GENERATE_REPORT 또는 OPENAI_FALLBACK_API_KEY "
            "(legacy OPENAI_API_KEY 포함)가 필요합니다."
        )
    return OpenAI(api_key=api_key)


def _is_profile_lock_error(error: Exception) -> bool:
    msg = str(error).lower()
    tokens = (
        "processsingleton",
        "user data directory is already in use",
        "profile appears to be in use",
        "another browser is using",
        "singletonlock",
        "lock file",
    )
    return any(token in msg for token in tokens)


def _is_browser_missing_error(error: Exception) -> bool:
    msg = str(error).lower()
    tokens = (
        "please run the following command to install new browsers",
        "playwright install",
        "executable doesn't exist",
        "browser has not been found",
    )
    return any(token in msg for token in tokens)


def _ensure_chromium_installed(phase: str) -> None:
    """런타임에서 Chromium이 누락된 경우 1회 자가 복구."""
    global _BROWSER_INSTALL_ATTEMPTED
    if _BROWSER_INSTALL_ATTEMPTED:
        return
    _BROWSER_INSTALL_ATTEMPTED = True
    logger.warning("[%s] Chromium 누락 감지 — playwright install chromium 시도", phase)
    result = subprocess.run(
        ["python3", "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(
            f"[{phase}] playwright install chromium 실패: {(stderr or stdout or f'code={result.returncode}')[:400]}"
        )
    logger.info("[%s] playwright install chromium 완료", phase)


def _launch_context_with_retry(playwright, headless: bool, phase: str, max_attempts: int = 12):
    """브라우저 프로필 잠금 충돌 시 재시도하며 context를 연다."""
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            # 다른 머신/이전 Chromium 실행에서 남긴 singleton lock 파일은
            # 프로필을 새 세션에서 여는 것 자체를 막으므로 선제적으로 제거한다.
            for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_path = BROWSER_PROFILE_DIR / lock_name
                if lock_path.exists() or lock_path.is_symlink():
                    try:
                        lock_path.unlink()
                    except Exception:
                        pass
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=headless,
                args=CHROMIUM_LAUNCH_ARGS,
                viewport=DEFAULT_VIEWPORT,
            )
            _apply_storage_state(context)
            return context
        except Exception as e:
            last_error = e
            if _is_browser_missing_error(e):
                _ensure_chromium_installed(phase)
                continue
            if not _is_profile_lock_error(e):
                raise
            wait_sec = min(10, 1 + attempt)
            logger.warning(
                "[%s] 브라우저 프로필 잠금 감지 (%d/%d): %s → %ds 후 재시도",
                phase, attempt, max_attempts, e, wait_sec
            )
            time.sleep(wait_sec)
    raise RuntimeError(f"[{phase}] 브라우저 프로필 잠금 해제 대기 실패: {last_error}")


def _apply_storage_state(context) -> None:
    """로컬에서 추출한 Playwright storage_state를 빈 Linux 프로필에 주입한다."""
    if not STORAGE_STATE_PATH.exists():
        return

    try:
        state = json.loads(STORAGE_STATE_PATH.read_text())
    except Exception as e:
        logger.warning("[storage_state] JSON 로드 실패: %s", e)
        return

    cookies = state.get("cookies") or []
    if cookies:
        try:
            context.add_cookies(cookies)
            logger.info("[storage_state] cookies %d개 적용", len(cookies))
        except Exception as e:
            logger.warning("[storage_state] cookie 적용 실패: %s", e)

    origins = state.get("origins") or []
    if not origins:
        return

    page = context.pages[0] if context.pages else context.new_page()
    for origin_entry in origins:
        origin = (origin_entry or {}).get("origin")
        local_storage = (origin_entry or {}).get("localStorage") or []
        if not origin or not local_storage:
            continue
        try:
            page.goto(origin, wait_until="domcontentloaded", timeout=20000)
            page.evaluate(
                """entries => {
                    for (const item of entries) {
                        window.localStorage.setItem(item.name, item.value);
                    }
                }""",
                local_storage,
            )
            logger.info("[storage_state] localStorage %d개 적용 origin=%s", len(local_storage), origin)
        except Exception as e:
            logger.warning("[storage_state] localStorage 적용 실패 origin=%s err=%s", origin, e)


def _parse_action_json(raw: str) -> dict:
    """모델 응답에서 JSON action 파싱."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    if not text.startswith("{"):
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            text = text[first:last + 1]

    action = json.loads(text)
    if not isinstance(action, dict):
        raise json.JSONDecodeError("action must be an object", text, 0)
    return action


def _normalize_report_title(title: str) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip())
    title = title.strip("-•|")
    return title


def _sanitize_collected_titles(raw_titles) -> list[str]:
    """collect 액션에서 받은 후보 제목을 정규화/필터링."""
    if not isinstance(raw_titles, list):
        return []

    noise = {
        "스튜디오",
        "Studio",
        "보고서",
        "직접 만들기",
        "소스",
        "채팅",
        "공유",
        "설정",
        "더보기",
    }
    time_only = re.compile(
        r"^(?:\d+\s*(?:초|분|시간|일|주|개월|달|년)\s*전|\d+\s*(?:sec|min|hour|day|week|month|year)s?\s*ago)$",
        re.IGNORECASE,
    )

    cleaned: list[str] = []
    for v in raw_titles:
        if not isinstance(v, str):
            continue
        t = _normalize_report_title(v)
        if not t or len(t) < 2 or len(t) > 160:
            continue
        if t in noise:
            continue
        if time_only.search(t):
            continue
        cleaned.append(t)

    return cleaned


def _extract_studio_report(page) -> str:
    """스튜디오 패널의 보고서 내용만 추출. 생성 중이거나 찾을 수 없으면 빈 문자열 반환."""
    try:
        body_text = page.inner_text("body")

        # 진단: '스튜디오' 주변 텍스트 출력
        studio_raw_pos = body_text.find('스튜디오')
        if studio_raw_pos >= 0:
            snippet = body_text[max(0, studio_raw_pos - 30):studio_raw_pos + 60]
            logger.info("[CUA] '스튜디오' 주변 텍스트: %r", snippet)
        else:
            logger.warning("[CUA] body text에 '스튜디오' 없음 — 전체 앞 500자: %r", body_text[:500])
            return ""

        # 진단: '기반:소스' 위치 확인
        attrib_raw_pos = body_text.find('기반:소스')
        if attrib_raw_pos >= 0:
            logger.info("[CUA] '기반:소스' 위치: %d (studio: %d)", attrib_raw_pos, studio_raw_pos)
        else:
            logger.warning("[CUA] body text에 '기반:소스' 없음")
            return ""

        # "스튜디오" 이후 ~ "기반:소스" 이전 구간 추출 (단순 find 사용)
        if attrib_raw_pos <= studio_raw_pos:
            logger.warning("[CUA] '기반:소스'가 '스튜디오' 앞에 있음 — 위치 역전")
            return ""

        content_area = body_text[studio_raw_pos:attrib_raw_pos]
        logger.info("[CUA] 스튜디오~기반:소스 구간: %d chars — %r", len(content_area), content_area[:200])

        # 스튜디오 네비게이션 버튼 제거 (20자 미만 라인), 보고서 본문만 유지
        lines = [l.strip() for l in content_area.split('\n') if len(l.strip()) > 20]
        report = '\n'.join(lines)

        if len(report) < 50:
            logger.info("[CUA] 스튜디오 콘텐츠 너무 짧음 (%d chars) — 아직 생성 중", len(report))
            return ""

        logger.info("[CUA] 스튜디오 보고서 추출 성공: %d chars", len(report))
        return report

    except Exception as e:
        logger.warning("[CUA] 스튜디오 추출 오류: %s", e)
        return ""


def _clean_extracted_report_text(text: str) -> str:
    """NotebookLM UI 잔여 줄을 제거하고 실제 보고서 본문만 남긴다."""
    if not text:
        return ""

    stop_prefixes = (
        "NotebookLM이 부정확한 정보를 표시할 수 있으므로",
    )
    blocked_exact = {
        "신고",
        "collapse_content",
        "more_horiz",
        "content_copy",
        "thumb_up",
        "thumb_down",
        "sticky_note_2",
        "메모 추가",
        "share",
        "공유",
        "keyboard_arrow_down",
        "chevron_right",
        "chevron_forward",
        "arrow_forward",
        "스튜디오",
        "소스 1개",
    }
    blocked_patterns = (
        r"^소스 \d+개 기반$",
        r"^기반:소스.*$",
        r"^소스 \d+개$",
    )

    cleaned_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(line.startswith(prefix) for prefix in stop_prefixes):
            break
        if line in blocked_exact:
            continue
        if re.fullmatch(r"[a-z_]+", line):
            continue
        if any(re.fullmatch(pattern, line) for pattern in blocked_patterns):
            continue
        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines).strip()
    if result != (text or "").strip():
        logger.info("[CUA] 보고서 정화 적용: before=%d after=%d chars", len(text.strip()), len(result))
    return result


def _extract_report_from_copy_toolbar(body_text: str) -> str:
    """새 보고서 뷰의 toolbar(content_copy) 기준으로 본문을 추출한다."""
    marker = "content_copy"
    marker_pos = body_text.find(marker)
    if marker_pos < 0:
        return ""

    text = body_text[marker_pos + len(marker):].lstrip()
    end_markers = [
        "\nthumb_up",
        "\nthumb_down",
        "\nNotebookLM이 부정확한 정보를 표시할 수 있으므로",
        "\nsticky_note_2",
        "\n메모 추가",
        "\n공유",
        "\nshare",
    ]
    end_positions = [text.find(m) for m in end_markers if text.find(m) > 0]
    if end_positions:
        text = text[:min(end_positions)]

    blocked_lines = {
        "신고",
        "collapse_content",
        "more_horiz",
        "content_copy",
        "thumb_up",
        "thumb_down",
        "sticky_note_2",
        "메모 추가",
        "share",
        "공유",
        "keyboard_arrow_down",
        "chevron_right",
        "chevron_forward",
        "arrow_forward",
    }
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in blocked_lines:
            continue
        if re.fullmatch(r"[a-z_]+", line):
            continue
        lines.append(line)

    result = "\n".join(lines).strip()
    if len(result) > 100:
        result = _clean_extracted_report_text(result)
        logger.info("[CUA] content_copy fallback 추출 성공: %d chars", len(result))
        return result
    return ""


def _extract_report_from_dom(page) -> str:
    """DOM에서 보고서 텍스트를 직접 추출. GPT OCR 대신 Playwright 사용."""
    # 1단계: 특정 CSS 셀렉터 시도
    js = """
    (selectors) => {
        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            if (els.length === 0) continue;
            const text = Array.from(els)
                .map(e => e.innerText.trim())
                .filter(t => t.length > 0)
                .join('\\n\\n');
            if (text.length > 100) return text;
        }
        return null;
    }
    """
    try:
        result = page.evaluate(js, _REPORT_SELECTORS)
        if result and len(result.strip()) > 100:
            result = _clean_extracted_report_text(result.strip())
            logger.info("[CUA] DOM 셀렉터 추출 성공: %d chars", len(result))
            return result
    except Exception as e:
        logger.warning("[CUA] DOM 셀렉터 추출 실패: %s", e)

    # 2단계: 스튜디오 패널 전용 추출
    report = _extract_studio_report(page)
    if report:
        return _clean_extracted_report_text(report)

    # 3단계: 구버전 NotebookLM 구조 호환
    try:
        body_text = page.inner_text("body")
        copy_toolbar_result = _extract_report_from_copy_toolbar(body_text)
        if copy_toolbar_result:
            return copy_toolbar_result
        match = re.search(
            r'소스 \d+개 기반\n(.+?)(?=\nthumb_up|\nNotebookLM이)',
            body_text,
            re.DOTALL,
        )
        if match:
            result = _clean_extracted_report_text(match.group(1).strip())
            logger.info("[CUA] 구버전 regex 추출 성공: %d chars", len(result))
            return result
    except Exception as e:
        logger.error("[CUA] 구버전 추출 실패: %s", e)

    logger.warning("[CUA] 모든 패턴 실패 — 빈 문자열 반환")
    return ""


def _body_contains_text(page, needle: str) -> bool:
    try:
        return needle in page.inner_text("body")
    except Exception as e:
        logger.warning("[CUA] body text 확인 실패 needle=%r err=%s", needle, e)
        return False


def execute_action(page, action: dict) -> bool:
    """액션 실행. done이면 True 반환."""
    t = action.get("action")
    if t == "click":
        page.mouse.click(action["x"], action["y"])
        time.sleep(0.8)
    elif t == "type":
        page.keyboard.type(action["text"])
        time.sleep(0.3)
    elif t == "key":
        key = action["key"].replace("Ctrl+", "Control+").replace("Ctrl", "Control")
        try:
            page.keyboard.press(key)
        except Exception as e:
            logger.warning("[CUA] key press 실패 (무시): key=%r error=%s", key, e)
        time.sleep(0.5)
    elif t == "scroll":
        page.mouse.move(action.get("x", 640), action.get("y", 400))
        page.mouse.wheel(0, action.get("delta_y", 300))
        time.sleep(0.3)
    elif t == "wait":
        time.sleep(action.get("ms", 2000) / 1000)
    elif t == "done":
        return True
    else:
        logger.warning("[CUA] 알 수 없는 액션: %s", t)
    return False


def _execute_list_action(page, action: dict, panel_anchor: Optional[dict] = None) -> bool:
    """list 모드 액션 실행. scroll은 Studio 패널 중심 좌표로 보정."""
    t = action.get("action")
    if t == "scroll":
        anchor_x = 1080
        anchor_y = 450
        if panel_anchor:
            anchor_x = int(panel_anchor.get("x", anchor_x))
            anchor_y = int(panel_anchor.get("y", anchor_y))
        delta = int(action.get("delta_y", 550))
        page.mouse.move(anchor_x, anchor_y)
        page.mouse.wheel(0, delta)
        time.sleep(0.45)
        return False

    return execute_action(page, action)


def _get_studio_panel_anchor(page) -> dict:
    """Studio 패널 중심점 추정. 실패 시 우측 패널 기본값 반환."""
    fallback = {"x": 1080, "y": 450}
    js = """
    () => {
      const candidates = [
        "[aria-label*='Studio']",
        "[aria-label*='스튜디오']",
        "[class*='studio']",
        "[class*='Studio']",
        "[data-testid*='studio']",
      ];
      for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 100 && r.height > 120) {
          return {x: Math.round(r.left + r.width * 0.5), y: Math.round(r.top + r.height * 0.6)};
        }
      }
      return null;
    }
    """
    try:
        pos = page.evaluate(js)
        if isinstance(pos, dict) and "x" in pos and "y" in pos:
            return {"x": int(pos["x"]), "y": int(pos["y"])}
    except Exception as e:
        logger.warning("[CUA][LIST] panel anchor 탐지 실패: %s", e)
    return fallback


def _ensure_studio_panel_visible(page) -> None:
    """Studio 패널/탭이 닫혀 있으면 가능한 셀렉터로 열기를 시도한다."""
    try:
        body_text = page.inner_text("body")
        existing_titles = _parse_report_titles(body_text)
        if existing_titles:
            logger.info("[list_reports] Studio 패널 내 제목이 이미 보임: %d개", len(existing_titles))
            return
        if "Studio" in body_text or "스튜디오" in body_text:
            logger.info("[list_reports] body text에 Studio 섹션 존재")
            return
    except Exception as e:
        logger.warning("[list_reports] Studio 패널 사전 확인 실패: %s", e)

    selectors = [
        "[role='tab'][aria-label*='Studio']",
        "[role='tab'][aria-label*='스튜디오']",
        "button[aria-label*='Studio']",
        "button[aria-label*='스튜디오']",
        "text=Studio",
        "text=스튜디오",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if not locator.is_visible(timeout=1000):
                continue
            locator.click(timeout=2000)
            logger.info("[list_reports] Studio 탭 열기 시도: %s", selector)
            time.sleep(1.0)
            return
        except Exception:
            continue


def _collect_titles_via_dom_scan(page, max_rounds: int = 4) -> list[str]:
    """Studio 패널을 몇 차례 스캔하며 body text parser로 제목을 수집한다."""
    panel_anchor = _get_studio_panel_anchor(page)
    seen = set()
    collected: list[str] = []
    no_new_rounds = 0

    for round_idx in range(max_rounds):
        try:
            body_text = page.inner_text("body")
        except Exception as e:
            logger.warning("[list_reports] DOM 스캔 body 읽기 실패(round=%d): %s", round_idx + 1, e)
            break

        titles = _parse_report_titles(body_text)
        before = len(collected)
        for title in titles:
            if title not in seen:
                seen.add(title)
                collected.append(title)

        added = len(collected) - before
        logger.info(
            "[list_reports] DOM 스캔 round=%d/%d added=%d total=%d",
            round_idx + 1,
            max_rounds,
            added,
            len(collected),
        )
        if added == 0:
            no_new_rounds += 1
        else:
            no_new_rounds = 0

        if no_new_rounds >= 2:
            break
        if round_idx < max_rounds - 1:
            _execute_list_action(page, {"action": "scroll", "delta_y": 640}, panel_anchor)

    return collected


def _click_first_visible(page, selectors: list[str], *, timeout_ms: int = 1500) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            logger.info("[dom] click selector=%s", selector)
            time.sleep(0.8)
            return True
        except Exception:
            continue
    return False


def _click_first_text(page, texts: list[str], *, exact: bool = True, timeout_ms: int = 1500) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=exact).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            logger.info("[dom] click text=%r", text)
            time.sleep(0.8)
            return True
        except Exception:
            continue
    return False


def _focus_first_visible(page, selectors: list[str], *, timeout_ms: int = 1500) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            logger.info("[dom] focus selector=%s", selector)
            time.sleep(0.5)
            return True
        except Exception:
            continue
    return False


def _is_prompt_input_visible(page, selectors: list[str], *, timeout_ms: int = 800) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            logger.info("[dom] prompt input visible selector=%s", selector)
            return True
        except Exception:
            continue
    return False


def _get_report_prompt_selectors() -> list[str]:
    """Fast Research/검색 입력창과 구분되는 보고서 맞춤설정 대화상자 전용 입력창 셀렉터."""
    return [
        "mat-dialog-container textarea[aria-label*='만들려는 보고서']",
        "mat-dialog-container textarea[placeholder*='새로운 웰니스 음료 출시']",
        "report-customization-dialog textarea[aria-label*='만들려는 보고서']",
        "report-customization-dialog textarea[placeholder*='새로운 웰니스 음료 출시']",
        "textarea[aria-label*='만들려는 보고서']",
        "textarea[placeholder*='새로운 웰니스 음료 출시']",
    ]


def _dismiss_blocking_dialogs(page) -> None:
    try:
        page.keyboard.press("Escape")
        time.sleep(0.3)
    except Exception:
        pass

    close_selectors = [
        "button[aria-label*='닫기']",
        "[role='button'][aria-label*='닫기']",
        "button[aria-label*='Close']",
        "[role='button'][aria-label*='Close']",
        "button:has-text('닫기')",
        "[role='button']:has-text('닫기')",
        "button:has-text('Close')",
        "[role='button']:has-text('Close')",
    ]
    _click_first_visible(page, close_selectors, timeout_ms=800)


def _try_dom_prepare_report_prompt(page) -> bool:
    """보고서 생성 입력창까지 DOM 셀렉터로 진입 시도."""
    _dismiss_blocking_dialogs(page)
    _ensure_studio_panel_visible(page)

    report_selectors = [
        "div[role='button'][aria-label='보고서']",
        "div[role='button'][aria-label*='보고서']",
        "button:has-text('보고서')",
        "[role='button']:has-text('보고서')",
        "[aria-label='보고서']",
        "[aria-label*='보고서']",
        "button:has-text('Report')",
        "[role='button']:has-text('Report')",
        "[aria-label='Report']",
        "[aria-label*='Report']",
    ]
    custom_selectors = [
        "button[aria-label='직접 만들기']",
        "button[aria-label*='직접 만들기']",
        "button:has-text('직접 만들기')",
        "[role='button']:has-text('직접 만들기')",
        "[aria-label='직접 만들기']",
        "button:has-text('사용자 지정')",
        "[role='button']:has-text('사용자 지정')",
        "[aria-label*='사용자 지정']",
        "button:has-text('맞춤')",
        "[role='button']:has-text('맞춤')",
        "button:has-text('직접 작성')",
        "[role='button']:has-text('직접 작성')",
        "button:has-text('Custom')",
        "[role='button']:has-text('Custom')",
        "[aria-label='Custom']",
        "[aria-label*='Custom']",
        "button:has-text('Make your own')",
        "[role='button']:has-text('Make your own')",
    ]
    input_selectors = _get_report_prompt_selectors()

    # 최근 UI에서는 custom 입력창이 이미 열린 상태로 시작할 수 있어
    # report/custom 카드 진입 전에 바로 포커스를 시도한다.
    if _is_prompt_input_visible(page, input_selectors, timeout_ms=1200):
        return _focus_first_visible(page, input_selectors, timeout_ms=1200)

    if not (
        _click_first_visible(page, report_selectors, timeout_ms=2500)
        or _click_first_text(page, ["보고서", "Report"], exact=True, timeout_ms=2500)
        or _click_first_text(page, ["보고서", "Report"], exact=False, timeout_ms=2500)
    ):
        logger.info("[dom] report tile not found")
        return False

    # 일부 UI에서는 보고서 카드 클릭 직후 곧바로 입력창이 열린다.
    if _is_prompt_input_visible(page, input_selectors, timeout_ms=1500):
        return _focus_first_visible(page, input_selectors, timeout_ms=1500)

    if not (
        _click_first_visible(page, custom_selectors, timeout_ms=2500)
        or _click_first_text(
            page,
            ["직접 만들기", "사용자 지정", "맞춤", "직접 작성", "Custom", "Make your own"],
            exact=True,
            timeout_ms=2500,
        )
        or _click_first_text(
            page,
            ["직접 만들기", "사용자 지정", "맞춤", "직접 작성", "Custom", "Make your own"],
            exact=False,
            timeout_ms=2500,
        )
    ):
        # custom 선택 버튼 문구가 바뀌었더라도 입력창이 이미 떴다면 성공으로 본다.
        if _is_prompt_input_visible(page, input_selectors, timeout_ms=1500):
            return _focus_first_visible(page, input_selectors, timeout_ms=1500)
        logger.info("[dom] custom option not found")
        return False
    if not _focus_first_visible(page, input_selectors, timeout_ms=2500):
        logger.info("[dom] prompt input not found")
        return False
    return True


def _try_dom_click_generate(page) -> bool:
    """생성 버튼을 DOM 셀렉터로 클릭 시도."""
    selectors = [
        "mat-dialog-container button:has-text('생성')",
        "report-customization-dialog button:has-text('생성')",
        "button:has-text('생성')",
        "[role='button']:has-text('생성')",
        "mat-dialog-container button:has-text('Generate')",
        "report-customization-dialog button:has-text('Generate')",
        "button:has-text('Generate')",
        "[role='button']:has-text('Generate')",
    ]
    return _click_first_visible(page, selectors, timeout_ms=2500)


def _sanitize_report_titles(titles: list[str]) -> list[str]:
    blocked = {
        "auto_tab_group",
        "chevron_forward",
        "chevron_right",
        "arrow_forward",
        "more_vert",
        "more_horiz",
        "보고서",
        "직접 만들기",
        "스튜디오",
        "Studio",
    }
    cleaned: list[str] = []
    seen = set()
    for title in titles:
        normalized = (title or "").strip()
        if not normalized or normalized in blocked or len(normalized) < 2:
            continue
        if re.fullmatch(r"[a-z_]+", normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    return cleaned


def _choose_generated_report_title(previous_titles: list[str], current_titles: list[str]) -> str:
    previous_set = set(_sanitize_report_titles(previous_titles))
    current_cleaned = _sanitize_report_titles(current_titles)
    for title in current_cleaned:
        if title not in previous_set:
            return title
    return current_cleaned[0] if current_cleaned else ""


def _try_dom_open_report_tile(page, target_title: str) -> bool:
    if not target_title:
        return False
    candidates = [
        page.get_by_text(target_title, exact=True).first,
        page.locator(f"[aria-label={json.dumps(target_title)}]").first,
    ]
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=2500)
            locator.click(timeout=2500)
            logger.info("[dom] report tile opened title=%r", target_title)
            time.sleep(1.0)
            return True
        except Exception:
            continue
    return False


def _open_report_tile(page, client, target_title: str, report_index: int = 0) -> None:
    if _try_dom_open_report_tile(page, target_title):
        return

    task_open_tile = (
        "Task: In the NotebookLM Studio panel, open the saved report tile that was just generated.\n"
        f"Target title: '{target_title[:80]}'\n"
        f"It is report number {report_index + 1} in the visible list.\n"
        "Steps:\n"
        "1. If the Studio panel is not open on the right, click the Studio tab.\n"
        "2. Find the target saved report tile.\n"
        "3. Click the tile to open its content.\n"
        '4. Output {"action":"done"} only when the report content view is open.\n'
        "Do NOT click the '보고서' generator card."
    )
    if not _run_cua_loop(page, client, task_open_tile, max_steps=12, phase="OPEN_TILE"):
        raise RuntimeError(f"생성된 보고서 타일 열기 실패: {target_title[:80]}")
    time.sleep(2)


def _run_cua_loop(
    page,
    client,
    task: str,
    max_steps: int,
    phase: str,
    allowed_actions: set = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> bool:
    """CUA 루프 실행 (모듈 레벨). done이면 True 반환."""
    HISTORY_WINDOW = 3
    msgs = [{"role": "system", "content": system_prompt}]
    for step in range(max_steps):
        _assert_allowed_url(page.url, phase)
        screenshot_b64 = base64.b64encode(page.screenshot()).decode()
        logger.info("[CUA][%s] 스텝 %d/%d — gpt-5.4 Vision 호출", phase, step + 1, max_steps)

        history = msgs[1:]
        if len(history) > HISTORY_WINDOW * 2:
            history = history[-(HISTORY_WINDOW * 2):]
        msgs = [msgs[0]] + history

        msgs.append({
            "role": "user",
            "content": [
                {"type": "text", "text": task},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                        "detail": "high",
                    },
                },
            ],
        })

        response = client.chat.completions.create(
            model="gpt-5.4",
            messages=msgs,
            max_completion_tokens=256,
            temperature=0,
        )
        _record_openai_usage(response, "gpt-5.4")

        raw = (response.choices[0].message.content or "").strip()
        logger.info("[CUA][%s] 모델 응답: %s", phase, raw[:200])

        try:
            action = _parse_action_json(raw)
        except json.JSONDecodeError as e:
            logger.error("[CUA][%s] JSON 파싱 실패: %s — %s", phase, e, raw)
            msgs.append({"role": "assistant", "content": raw})
            continue

        if allowed_actions and action.get("action") not in allowed_actions:
            logger.warning("[CUA][%s] 허용되지 않은 액션 차단 → wait: %s", phase, action)
            action = {"action": "wait", "ms": 3000}

        msgs.append({"role": "assistant", "content": raw})
        logger.info("[CUA][%s] 액션: %s", phase, action)

        if execute_action(page, action):
            return True

    return False


def _run_list_cua_loop(
    page,
    client,
    task: str,
    max_steps: int,
    max_no_new_rounds: int = 2,
    min_steps_before_done: int = 4,
    min_scrolls_for_empty: int = 3,
    min_elapsed_sec_for_empty: float = 8.0,
    max_model_errors: int = 4,
) -> tuple[list[str], dict]:
    """list 전용 CUA 루프: collect/scroll/done 기반으로 보고서 제목을 누적 수집."""
    HISTORY_WINDOW = 4
    msgs = [{"role": "system", "content": LIST_SYSTEM_PROMPT}]
    collected: list[str] = []
    seen = set()
    no_new_rounds = 0
    scroll_count = 0
    collect_attempts = 0
    empty_collect_rounds = 0
    premature_done_count = 0
    start_ts = time.monotonic()
    panel_anchor = _get_studio_panel_anchor(page)
    meta = {
        "steps": 0,
        "parse_errors": 0,
        "invalid_actions": 0,
        "model_errors": 0,
        "done": False,
        "elapsed_sec": 0.0,
        "scroll_count": 0,
        "collect_attempts": 0,
        "premature_done_count": 0,
        "termination_reason": "max_steps",
        "empty_result_accepted": False,
    }

    for step in range(max_steps):
        meta["steps"] = step + 1
        _assert_allowed_url(page.url, "LIST")
        screenshot_b64 = base64.b64encode(page.screenshot()).decode()
        logger.info("[CUA][LIST] 스텝 %d/%d — collect=%d no_new=%d", step + 1, max_steps, len(collected), no_new_rounds)

        history = msgs[1:]
        if len(history) > HISTORY_WINDOW * 2:
            history = history[-(HISTORY_WINDOW * 2):]
        msgs = [msgs[0]] + history

        instruction = (
            f"{task}\n\n"
            f"Current progress: already collected {len(collected)} unique titles.\n"
            "If visible titles exist, prefer collect.\n"
            "When no more new titles are discoverable, return done."
        )

        msgs.append({
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                        "detail": "high",
                    },
                },
            ],
        })

        try:
            response = client.chat.completions.create(
                model="gpt-5.4",
                messages=msgs,
                max_completion_tokens=400,
                temperature=0,
            )
            _record_openai_usage(response, "gpt-5.4")
        except Exception as e:
            meta["model_errors"] = int(meta["model_errors"]) + 1
            no_new_rounds += 1
            logger.warning(
                "[CUA][LIST] 모델 호출 실패 (%d/%d): %s",
                meta["model_errors"],
                max_model_errors,
                e,
            )
            _execute_list_action(page, {"action": "wait", "ms": 1500}, panel_anchor)
            if int(meta["model_errors"]) >= max_model_errors:
                _execute_list_action(page, {"action": "scroll", "delta_y": 640}, panel_anchor)
                scroll_count += 1
            if int(meta["model_errors"]) >= (max_model_errors + 2):
                meta["termination_reason"] = "model_errors_exceeded"
                break
            continue

        raw = (response.choices[0].message.content or "").strip()
        logger.info("[CUA][LIST] 모델 응답: %s", raw[:300])

        try:
            action = _parse_action_json(raw)
        except json.JSONDecodeError as e:
            logger.error("[CUA][LIST] JSON 파싱 실패: %s — %s", e, raw)
            meta["parse_errors"] = int(meta["parse_errors"]) + 1
            msgs.append({"role": "assistant", "content": raw})
            no_new_rounds += 1
            if no_new_rounds >= max_no_new_rounds + 2:
                logger.warning("[CUA][LIST] 연속 JSON 파싱 실패로 중단")
                break
            continue

        at = action.get("action")
        if at not in {"collect", "scroll", "wait", "done", "click"}:
            logger.warning("[CUA][LIST] 허용되지 않은 액션: %s", action)
            meta["invalid_actions"] = int(meta["invalid_actions"]) + 1
            action = {"action": "wait", "ms": 1200}
            at = "wait"

        msgs.append({"role": "assistant", "content": raw})
        logger.info("[CUA][LIST] 액션: %s", action)

        if at == "collect":
            collect_attempts += 1
            before = len(collected)
            titles = _sanitize_collected_titles(action.get("titles", []))
            for t in titles:
                if t not in seen:
                    seen.add(t)
                    collected.append(t)
            added = len(collected) - before
            logger.info("[CUA][LIST] collect 추가=%d 총=%d", added, len(collected))
            if added == 0:
                empty_collect_rounds += 1
                no_new_rounds += 1
            else:
                empty_collect_rounds = 0
                no_new_rounds = 0
        elif at == "done":
            elapsed_sec = time.monotonic() - start_ts
            can_accept_done = (
                len(collected) > 0
                or (
                    step + 1 >= min_steps_before_done
                    and scroll_count >= min_scrolls_for_empty
                    and elapsed_sec >= min_elapsed_sec_for_empty
                    and collect_attempts >= min_steps_before_done // 2
                )
            )
            if can_accept_done:
                logger.info("[CUA][LIST] done 수신 — 루프 종료 (accepted)")
                meta["done"] = True
                meta["termination_reason"] = "done"
                meta["empty_result_accepted"] = len(collected) == 0
                break
            premature_done_count += 1
            no_new_rounds += 1
            logger.warning(
                "[CUA][LIST] premature done 무시 (step=%d collected=%d scroll=%d elapsed=%.2fs)",
                step + 1, len(collected), scroll_count, elapsed_sec,
            )
            _execute_list_action(page, {"action": "scroll", "delta_y": 620}, panel_anchor)
            scroll_count += 1
        else:
            # scroll/wait/click만 execute_action에 위임
            _execute_list_action(page, action, panel_anchor)
            if at == "scroll":
                no_new_rounds += 1
                scroll_count += 1

        elapsed_sec = time.monotonic() - start_ts
        can_accept_empty = (
            step + 1 >= min_steps_before_done
            and scroll_count >= min_scrolls_for_empty
            and elapsed_sec >= min_elapsed_sec_for_empty
            and collect_attempts >= min_steps_before_done // 2
        )
        if no_new_rounds >= max_no_new_rounds:
            if collected:
                logger.info("[CUA][LIST] 신규 제목 없음 연속 %d회 — 종료(제목 확보)", no_new_rounds)
                meta["termination_reason"] = "stagnation_with_titles"
                break
            if can_accept_empty:
                logger.info("[CUA][LIST] 신규 제목 없음 연속 %d회 — 종료(빈목록 승인)", no_new_rounds)
                meta["termination_reason"] = "stagnation_empty_accepted"
                meta["empty_result_accepted"] = True
                break
            logger.info("[CUA][LIST] 탐색 부족 상태로 no_new 도달 — 탐색 연장")
            no_new_rounds = max_no_new_rounds - 1
            _execute_list_action(page, {"action": "scroll", "delta_y": 680}, panel_anchor)
            scroll_count += 1

    meta["elapsed_sec"] = round(time.monotonic() - start_ts, 2)
    meta["scroll_count"] = scroll_count
    meta["collect_attempts"] = collect_attempts
    meta["premature_done_count"] = premature_done_count
    meta["empty_collect_rounds"] = empty_collect_rounds
    if meta.get("termination_reason") == "max_steps" and meta.get("done"):
        meta["termination_reason"] = "done"

    logger.info(
        "[CUA][LIST] 종료 요약 reason=%s steps=%s elapsed=%.2fs titles=%d scroll=%d collect=%d premature_done=%d model_errors=%d empty_ok=%s",
        meta.get("termination_reason"),
        meta.get("steps"),
        meta.get("elapsed_sec"),
        len(collected),
        scroll_count,
        collect_attempts,
        premature_done_count,
        meta.get("model_errors"),
        meta.get("empty_result_accepted"),
    )
    return collected, meta


def _parse_report_titles(body_text: str) -> list[str]:
    """body text에서 스튜디오 보고서 타일 제목 목록을 파싱. 페이지 이동 없음."""
    try:
        section_start = -1
        for keyword in ("스튜디오", "Studio"):
            pos = body_text.find(keyword)
            if pos >= 0:
                section_start = pos
                break

        if section_start < 0:
            logger.warning("[parse_titles] studio section not found")
            return []

        studio_section = body_text[section_start:]
        lines = [l.strip() for l in studio_section.split("\n") if l.strip()]
        logger.info("[parse_titles] studio lines=%d", len(lines))

        blacklist = {
            "스튜디오",
            "Studio",
            "보고서",
            "직접 만들기",
            "auto_tab_group",
            "chevron_forward",
            "chevron_right",
            "arrow_forward",
            "more_vert",
            "more_horiz",
            "소스",
            "채팅",
            "공유",
            "설정",
            "더보기",
        }
        time_pattern = re.compile(
            r"(?:\d+\s*(?:초|분|시간|일|주|개월|달|년)\s*전|\d+\s*(?:sec|min|hour|day|week|month|year)s?\s*ago)",
            re.IGNORECASE,
        )

        titles: list[str] = []

        # 1) 시간 라벨 기준 역추적 (UI 변경에 비교적 강함)
        for i, line in enumerate(lines):
            if not time_pattern.search(line):
                continue
            for offset in (1, 2, 3):
                if i - offset < 0:
                    continue
                candidate = lines[i - offset].strip()
                if (
                    len(candidate) < 2
                    or len(candidate) > 120
                    or candidate in blacklist
                    or time_pattern.search(candidate)
                ):
                    continue
                titles.append(candidate)
                break

        # 2) 텍스트 페어 패턴 fallback (title\nN시간 전)
        if not titles:
            pair_pattern = re.compile(
                r"(?P<title>[^\n]{2,120})\n(?P<time>(?:\d+\s*(?:초|분|시간|일|주|개월|달|년)\s*전|\d+\s*(?:sec|min|hour|day|week|month|year)s?\s*ago))",
                re.IGNORECASE,
            )
            for m in pair_pattern.finditer(studio_section):
                candidate = (m.group("title") or "").strip()
                if candidate and candidate not in blacklist:
                    titles.append(candidate)

        # 중복 제거 (순서 유지)
        deduped: list[str] = []
        seen = set()
        for t in titles:
            key = t.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)

        logger.info("[parse_titles] parsed titles=%d", len(deduped))
        return deduped
    except Exception as e:
        logger.exception("[parse_titles] unexpected error: %s", e)
        return []


def list_reports(page, notebook_url: str) -> list[str]:
    """NotebookLM 스튜디오 패널에서 기존 보고서 타일 제목 목록을 반환."""
    logger.info("[list_reports] 노트북 이동 중: %s", notebook_url)
    page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        logger.warning("[list_reports] networkidle 타임아웃: %s", e)

    client = _build_openai_client()
    _ensure_logged_in(page, client)
    time.sleep(2)
    _ensure_studio_panel_visible(page)

    dom_titles = _collect_titles_via_dom_scan(page, max_rounds=4)
    if dom_titles:
        logger.info("[list_reports] DOM 스캔 목록 수집 성공: %d개", len(dom_titles))
        return dom_titles

    task = (
        "Task: In NotebookLM Studio, collect titles of saved reports already listed in the Studio panel.\n"
        "Steps:\n"
        "1. If Studio panel is not visible, open the Studio tab on the right.\n"
        "2. Read visible saved report/document cards and return them with collect.\n"
        "3. Scroll inside the Studio panel to discover more titles when needed.\n"
        "4. Return done when no additional new titles are discoverable.\n"
        "Important:\n"
        "- Never click '보고서' generation tile for creating a new report.\n"
        "- Never open a report tile in list mode.\n"
        "- Only list existing saved report titles."
    )

    list_max_steps = 16
    list_max_no_new_rounds = 4
    list_min_steps_before_done = 5
    list_min_scrolls_for_empty = 4
    list_min_elapsed_sec_for_empty = 12.0

    cua_titles: list[str] = []
    cua_meta = {
        "steps": 0,
        "parse_errors": 0,
        "invalid_actions": 0,
        "done": False,
    }
    cua_loop_error = ""
    try:
        cua_titles, cua_meta = _run_list_cua_loop(
            page,
            client,
            task=task,
            max_steps=list_max_steps,
            max_no_new_rounds=list_max_no_new_rounds,
            min_steps_before_done=list_min_steps_before_done,
            min_scrolls_for_empty=list_min_scrolls_for_empty,
            min_elapsed_sec_for_empty=list_min_elapsed_sec_for_empty,
        )
    except Exception as e:
        cua_loop_error = str(e)
        logger.exception("[list_reports] CUA list loop 실패: %s", e)

    if cua_titles:
        logger.info(
            "[list_reports] CUA 목록 수집 성공: %d개 (steps=%s done=%s parse_errors=%s invalid_actions=%s)",
            len(cua_titles),
            cua_meta.get("steps"),
            cua_meta.get("done"),
            cua_meta.get("parse_errors"),
            cua_meta.get("invalid_actions"),
        )
        return cua_titles

    logger.warning(
        "[list_reports] CUA 목록이 비어 fallback parser 사용 (steps=%s done=%s parse_errors=%s invalid_actions=%s model_errors=%s err=%s)",
        cua_meta.get("steps"),
        cua_meta.get("done"),
        cua_meta.get("parse_errors"),
        cua_meta.get("invalid_actions"),
        cua_meta.get("model_errors"),
        cua_loop_error[:120],
    )
    try:
        _ensure_studio_panel_visible(page)
        dom_titles = _collect_titles_via_dom_scan(page, max_rounds=3)
        if dom_titles:
            logger.warning("[list_reports] CUA 이후 DOM 스캔 경로로 목록 반환됨")
            return dom_titles
        body_text = page.inner_text("body")
        fallback_titles = _parse_report_titles(body_text)
        logger.info("[list_reports] fallback 목록 수집 결과: %d개", len(fallback_titles))
        if fallback_titles:
            logger.warning("[list_reports] fallback parser 경로로 목록 반환됨")
            return fallback_titles
        if cua_loop_error:
            if "step limit" in cua_loop_error.lower():
                logger.warning("[list_reports] step limit 도달 + DOM/Fallback 빈결과 — 빈 목록으로 처리")
                return []
            raise RuntimeError(f"CUA list loop failed: {cua_loop_error}")
        if bool(cua_meta.get("empty_result_accepted")):
            logger.info("[list_reports] CUA 빈목록 승인 조건 충족 — 빈 배열 반환")
            return []
        if int(cua_meta.get("parse_errors") or 0) >= list_max_no_new_rounds:
            raise RuntimeError("CUA list loop failed: repeated JSON parse errors")
        if int(cua_meta.get("invalid_actions") or 0) >= list_max_no_new_rounds:
            raise RuntimeError("CUA list loop failed: repeated invalid actions")
        if int(cua_meta.get("model_errors") or 0) >= 4:
            raise RuntimeError("CUA list loop failed: repeated model call errors")
        if not bool(cua_meta.get("done")) and int(cua_meta.get("steps") or 0) >= list_max_steps:
            logger.warning("[list_reports] max_steps 종료 + DOM/Fallback 빈결과 — 빈 목록으로 처리")
            return []
        raise RuntimeError(
            "CUA list loop did not gather titles and empty-result acceptance criteria were not met"
        )
    except Exception as e:
        logger.exception("[list_reports] fallback parser 실패: %s", e)
        raise


def get_existing_report(page, notebook_url: str, report_index: int, output_path: str) -> str:
    """GPT-5.4 CUA로 기존 보고서 타일을 클릭해서 내용을 추출하고 파일로 저장한다."""
    logger.info("[get_existing_report] index=%d url=%s", report_index, notebook_url)
    client = _build_openai_client()

    page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        logger.warning("[get_existing_report] networkidle 타임아웃: %s", e)

    _ensure_logged_in(page, client)
    time.sleep(2)
    _ensure_studio_panel_visible(page)

    # 타일 목록 파싱 (제목 확인용, navigate 없음)
    titles = _collect_titles_via_dom_scan(page, max_rounds=3)
    if not titles:
        titles = _parse_report_titles(page.inner_text("body"))
    if not titles:
        raise RuntimeError("보고서 목록이 비어 있습니다.")
    if report_index >= len(titles):
        raise RuntimeError(f"report_index={report_index} out of range (총 {len(titles)}개)")

    target_title = titles[report_index]
    logger.info("[get_existing_report] 대상 [%d]: %r", report_index, target_title)

    # CUA Phase: 타일 클릭 (GPT-5.4 vision이 UI를 보고 올바른 타일 클릭)
    TASK_OPEN_TILE = (
        f"Task: In the NotebookLM Studio panel, find and click the saved report/document tile.\n"
        f"Target title: '{target_title[:80]}'\n"
        f"It is report number {report_index + 1} in the list.\n"
        "Steps:\n"
        "1. If the Studio panel is not open on the right, click the Studio tab\n"
        "2. Scroll down in the Studio panel if needed to find the tile\n"
        f"3. Click on the tile titled '{target_title[:60]}'\n"
        "4. Wait for the report content to appear\n"
        f'Output {{"action": "done"}} when the report content is fully visible on screen.\n'
        "Do NOT generate a new report — only open an existing one."
    )

    if not _run_cua_loop(page, client, TASK_OPEN_TILE, max_steps=12, phase="GET_TILE"):
        raise RuntimeError("CUA 타일 클릭 실패 (12 스텝 초과)")

    time.sleep(3)

    # 추출: CSS 셀렉터 → 스튜디오 패턴 → 구버전 순으로 시도
    report_text = ""
    for attempt in range(12):
        report_text = _extract_report_from_dom(page)
        if report_text:
            logger.info("[get_existing_report] 추출 성공 (시도 %d/12)", attempt + 1)
            break
        logger.info("[get_existing_report] 추출 재시도 %d/12", attempt + 1)
        time.sleep(5)

    if not report_text:
        raise RuntimeError("기존 보고서 DOM 추출 결과 없음")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(report_text, encoding="utf-8")
    logger.info("[get_existing_report] 저장 완료: %s (%d chars)", output_path, len(report_text))
    return output_path


def generate_report(prompt: str, notebook_url: str, output_path: str, headless: bool = True) -> str:
    logger.info("[CUA] 시작 prompt=%r url=%s headless=%s", prompt, notebook_url, headless)
    client = _build_openai_client()
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # Phase 1: 입력 필드 포커스까지만 — 프롬프트 텍스트 노출 없음
    TASK_PHASE1 = (
        "Task: Open the custom report input dialog in NotebookLM.\n"
        "Steps:\n"
        "1. If the Studio panel is not visible, click the Studio tab (right side)\n"
        "2. Click the '보고서' (report) tile\n"
        "3. Click '직접 만들기' (Custom) option\n"
        "4. Click inside the prompt text input field so it is focused\n"
        "Output {\"action\": \"done\"} when the text input field is focused and ready for input.\n"
        "Do NOT type anything yet — just navigate to and focus the input field."
    )

    # Phase 3a: Generate 버튼 클릭만 — 클릭 후 즉시 done
    TASK_PHASE3_CLICK = (
        "Task: Click the Generate (생성) button.\n"
        "The custom prompt text has already been typed in the input field.\n"
        "Find the blue Generate/생성 button and click it.\n"
        "Output {\"action\": \"done\"} immediately after clicking.\n"
        "Do NOT wait for the report to finish — just click Generate and output done."
    )

    # Phase 3b는 GPT 없이 Playwright 네이티브 대기로 처리
    PHASE3B_WAIT_MS = 180000  # 최대 3분

    with sync_playwright() as p:
        logger.info("[CUA] Chromium 시작")
        context = _launch_context_with_retry(p, headless=headless, phase="GENERATE")
        page = context.new_page()
        logger.info("[CUA] 노트북 URL 이동 중...")
        page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        _assert_allowed_url(page.url, "NAVIGATE")
        logger.info("[CUA] 페이지 로드 완료: %s / url=%s", page.title(), page.url)

        _ensure_logged_in(page, client)
        time.sleep(2)
        existing_titles_before = _sanitize_report_titles(_collect_titles_via_dom_scan(page, max_rounds=2))
        logger.info("[CUA] 생성 전 보고서 목록: %d개", len(existing_titles_before))

        # Phase 1: 입력 필드까지 내비게이션 (프롬프트 텍스트 GPT에 노출 안 함)
        logger.info("[CUA] Phase 1 시작: 보고서 입력 필드로 내비게이션")
        if _try_dom_prepare_report_prompt(page):
            logger.info("[CUA] Phase 1 완료: DOM 경로로 입력 필드 포커스됨")
        else:
            if not _run_cua_loop(page, client, TASK_PHASE1, max_steps=25, phase="P1"):
                # CUA가 한 차례 실패한 뒤에도 DOM 경로가 다시 열릴 수 있어 재확인한다.
                _dismiss_blocking_dialogs(page)
                time.sleep(1)
                if _try_dom_prepare_report_prompt(page):
                    logger.info("[CUA] Phase 1 완료: CUA 실패 후 DOM 재시도로 입력 필드 포커스됨")
                else:
                    context.close()
                    raise RuntimeError("Phase 1 실패: 입력 필드 포커스 불가 (DOM/CUA 재시도 후에도 실패)")
            else:
                logger.info("[CUA] Phase 1 완료: CUA 경로로 입력 필드 포커스됨")

        # Phase 2: Playwright로 직접 프롬프트 입력 (GPT에 프롬프트 텍스트 비노출)
        logger.info("[CUA] Phase 2: 프롬프트 직접 입력 (%d chars)", len(prompt))
        page.keyboard.type(prompt)
        time.sleep(1)
        logger.info("[CUA] Phase 2 완료: 프롬프트 입력됨")

        # Phase 3a: Generate 버튼 클릭
        logger.info("[CUA] Phase 3a 시작: Generate 버튼 클릭")
        if _try_dom_click_generate(page):
            logger.info("[CUA] Phase 3a 완료: DOM 경로로 Generate 버튼 클릭됨")
        else:
            if not _run_cua_loop(page, client, TASK_PHASE3_CLICK, max_steps=5, phase="P3a"):
                context.close()
                raise RuntimeError("Phase 3a 실패: Generate 버튼 클릭 불가 (5 스텝 초과)")
            logger.info("[CUA] Phase 3a 완료: CUA 경로로 Generate 버튼 클릭됨")
        time.sleep(3)  # 생성 시작 대기

        # Phase 3b: Playwright 네이티브 대기 (2단계)
        # Step 1: "보고서 생성 중" 나타날 때까지 기다림 (생성이 시작됐는지 확인)
        logger.info("[CUA] Phase 3b-1: '보고서 생성 중' 나타날 때까지 대기 (최대 30초)")
        loading_seen = False
        for _ in range(30):
            if _body_contains_text(page, "보고서 생성 중"):
                loading_seen = True
                break
            time.sleep(1)
        if loading_seen:
            logger.info("[CUA] Phase 3b-1: '보고서 생성 중' 감지됨 — 생성 시작 확인")
        else:
            logger.warning("[CUA] Phase 3b-1: '보고서 생성 중' 30초 내 미감지 — 이미 완료됐거나 다른 상태")

        # Step 2: "보고서 생성 중" 사라질 때까지 기다림 (생성 완료 확인)
        logger.info("[CUA] Phase 3b-2: '보고서 생성 중' 사라질 때까지 대기 (최대 %dms)", PHASE3B_WAIT_MS)
        loading_cleared = not loading_seen
        deadline = time.monotonic() + (PHASE3B_WAIT_MS / 1000)
        while time.monotonic() < deadline:
            if not _body_contains_text(page, "보고서 생성 중"):
                loading_cleared = True
                break
            time.sleep(2)
        if loading_cleared:
            logger.info("[CUA] Phase 3b-2 완료: '보고서 생성 중' 사라짐")
        else:
            logger.warning("[CUA] Phase 3b-2 타임아웃 (%dms) — 강제 추출 시도", PHASE3B_WAIT_MS)

        time.sleep(3)  # 렌더링 안정화
        current_titles = _sanitize_report_titles(_collect_titles_via_dom_scan(page, max_rounds=2))
        target_title = _choose_generated_report_title(existing_titles_before, current_titles)
        logger.info(
            "[CUA] 생성 후 보고서 목록: before=%d after=%d target=%r",
            len(existing_titles_before),
            len(current_titles),
            target_title,
        )
        if target_title:
            _open_report_tile(page, client, target_title, report_index=0)

        # 보고서 DOM 폴링 (최대 120초, 5초 간격)
        report_text = ""
        for attempt in range(24):
            report_text = _extract_report_from_dom(page)
            if report_text:
                logger.info("[CUA] 보고서 DOM 확인됨 (시도 %d/%d)", attempt + 1, 24)
                break
            logger.info("[CUA] 추출 재시도 %d/24 — 보고서 DOM 대기 중...", attempt + 1)
            time.sleep(5)

        context.close()

        if not report_text:
            raise RuntimeError("보고서 DOM 추출 결과 없음")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(report_text, encoding="utf-8")
        logger.info("[CUA] 보고서 저장 완료: %s (%d chars)", output_path, len(report_text))
        return output_path


def main():
    set_cost_tracker_api_key_family("cua_generate_report")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="generate", choices=["generate", "list", "get"])
    parser.add_argument("--prompt", default="")
    parser.add_argument("--notebook-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report-index", type=int, default=0)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    if args.mode == "generate":
        if not args.prompt:
            parser.error("--prompt is required for --mode generate")
        result = generate_report(args.prompt, args.notebook_url, args.output, args.headless)
        print(f"✅ {result}")

    elif args.mode == "list":
        with sync_playwright() as p:
            context = _launch_context_with_retry(p, headless=args.headless, phase="LIST")
            page = context.new_page()
            titles = list_reports(page, args.notebook_url)
            context.close()

        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
        print(f"✅ {len(titles)} reports listed → {args.output}")

    elif args.mode == "get":
        with sync_playwright() as p:
            context = _launch_context_with_retry(p, headless=args.headless, phase="GET")
            page = context.new_page()
            result = get_existing_report(page, args.notebook_url, args.report_index, args.output)
            context.close()
        print(f"✅ {result}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error("[CUA][FATAL] %s", e)
        traceback.print_exc()
        emit_cost_tracking_summary()
        raise
    else:
        emit_cost_tracking_summary()
