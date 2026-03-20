import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

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


def _ensure_logged_in(page: Page) -> None:
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

    try:
        # 이메일 입력
        page.wait_for_selector("input[type='email']", timeout=15000)
        page.fill("input[type='email']", email)
        page.click("button:has-text('다음'), button:has-text('Next'), #identifierNext")
        logger.info("[login] 이메일 입력 완료")

        # 비밀번호 입력
        page.wait_for_selector("input[type='password']", timeout=15000)
        time.sleep(0.5)
        page.fill("input[type='password']", password)
        page.click("button:has-text('다음'), button:has-text('Next'), #passwordNext")
        logger.info("[login] 비밀번호 입력 완료")

        # NotebookLM 리디렉션 대기 (최대 30초)
        page.wait_for_url("**/notebooklm.google.com/**", timeout=30000)
        logger.info("[login] 로그인 성공 — url=%s", page.url)

    except Exception as e:
        logger.error("[login] 자동 로그인 실패: %s", e)
        logger.error("[login] 현재 URL: %s", page.url)
        raise RuntimeError(f"Google 자동 로그인 실패: {e}")


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
            logger.info("[CUA] DOM 셀렉터 추출 성공: %d chars", len(result))
            return result.strip()
    except Exception as e:
        logger.warning("[CUA] DOM 셀렉터 추출 실패: %s", e)

    # 2단계: 스튜디오 패널 전용 추출
    report = _extract_studio_report(page)
    if report:
        return report

    # 3단계: 구버전 NotebookLM 구조 호환
    try:
        body_text = page.inner_text("body")
        match = re.search(
            r'소스 \d+개 기반\n(.+?)(?=\nthumb_up|\nNotebookLM이)',
            body_text,
            re.DOTALL,
        )
        if match:
            result = match.group(1).strip()
            logger.info("[CUA] 구버전 regex 추출 성공: %d chars", len(result))
            return result
    except Exception as e:
        logger.error("[CUA] 구버전 추출 실패: %s", e)

    logger.warning("[CUA] 모든 패턴 실패 — 빈 문자열 반환")
    return ""


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


def _run_cua_loop(
    page,
    client,
    task: str,
    max_steps: int,
    phase: str,
    allowed_actions: set = None,
) -> bool:
    """CUA 루프 실행 (모듈 레벨). done이면 True 반환."""
    HISTORY_WINDOW = 3
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for step in range(max_steps):
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

        raw = (response.choices[0].message.content or "").strip()
        logger.info("[CUA][%s] 모델 응답: %s", phase, raw[:200])

        try:
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            action = json.loads(raw.strip())
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


def _parse_report_titles(body_text: str) -> list[str]:
    """body text에서 스튜디오 보고서 타일 제목 목록을 파싱. 페이지 이동 없음."""
    studio_pos = body_text.find("스튜디오")
    if studio_pos < 0:
        return []

    studio_section = body_text[studio_pos:]
    lines = [l.strip() for l in studio_section.split("\n") if l.strip()]

    time_pattern = re.compile(r"\d+[시분일주]간?\s*전")
    titles = []
    for i, line in enumerate(lines):
        if time_pattern.search(line) and i > 0:
            candidate = lines[i - 1]
            if len(candidate) > 3 and candidate not in ("스튜디오", "보고서", "직접 만들기"):
                titles.append(candidate)
    return titles


def list_reports(page, notebook_url: str) -> list[str]:
    """NotebookLM 스튜디오 패널에서 기존 보고서 타일 제목 목록을 반환."""
    logger.info("[list_reports] 노트북 이동 중: %s", notebook_url)
    page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        logger.warning("[list_reports] networkidle 타임아웃: %s", e)

    _ensure_logged_in(page)
    time.sleep(2)

    titles = _parse_report_titles(page.inner_text("body"))
    logger.info("[list_reports] 보고서 %d개 발견: %s", len(titles), titles)
    return titles


def get_existing_report(page, notebook_url: str, report_index: int, output_path: str) -> str:
    """GPT-5.4 CUA로 기존 보고서 타일을 클릭해서 내용을 추출하고 파일로 저장한다."""
    logger.info("[get_existing_report] index=%d url=%s", report_index, notebook_url)
    client = OpenAI()

    page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        logger.warning("[get_existing_report] networkidle 타임아웃: %s", e)

    _ensure_logged_in(page)
    time.sleep(2)

    # 타일 목록 파싱 (제목 확인용, navigate 없음)
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
    client = OpenAI()
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
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        logger.info("[CUA] 노트북 URL 이동 중...")
        page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        logger.info("[CUA] 페이지 로드 완료: %s / url=%s", page.title(), page.url)

        _ensure_logged_in(page)
        time.sleep(2)

        # Phase 1: 입력 필드까지 내비게이션 (프롬프트 텍스트 GPT에 노출 안 함)
        logger.info("[CUA] Phase 1 시작: 보고서 입력 필드로 내비게이션")
        if not _run_cua_loop(page, client, TASK_PHASE1, max_steps=15, phase="P1"):
            context.close()
            raise RuntimeError("Phase 1 실패: 입력 필드 포커스 불가 (15 스텝 초과)")
        logger.info("[CUA] Phase 1 완료: 입력 필드 포커스됨")

        # Phase 2: Playwright로 직접 프롬프트 입력 (GPT에 프롬프트 텍스트 비노출)
        logger.info("[CUA] Phase 2: 프롬프트 직접 입력 (%d chars)", len(prompt))
        page.keyboard.type(prompt)
        time.sleep(1)
        logger.info("[CUA] Phase 2 완료: 프롬프트 입력됨")

        # Phase 3a: Generate 버튼 클릭
        logger.info("[CUA] Phase 3a 시작: Generate 버튼 클릭")
        if not _run_cua_loop(page, client, TASK_PHASE3_CLICK, max_steps=5, phase="P3a"):
            context.close()
            raise RuntimeError("Phase 3a 실패: Generate 버튼 클릭 불가 (5 스텝 초과)")
        logger.info("[CUA] Phase 3a 완료: Generate 버튼 클릭됨")
        time.sleep(3)  # 생성 시작 대기

        # Phase 3b: Playwright 네이티브 대기 (2단계)
        # Step 1: "보고서 생성 중" 나타날 때까지 기다림 (생성이 시작됐는지 확인)
        logger.info("[CUA] Phase 3b-1: '보고서 생성 중' 나타날 때까지 대기 (최대 30초)")
        try:
            page.wait_for_function(
                "() => document.body.innerText.includes('보고서 생성 중')",
                timeout=30000,
            )
            logger.info("[CUA] Phase 3b-1: '보고서 생성 중' 감지됨 — 생성 시작 확인")
        except Exception as e:
            logger.warning("[CUA] Phase 3b-1: '보고서 생성 중' 30초 내 미감지 (%s) — 이미 완료됐거나 다른 상태", e)

        # Step 2: "보고서 생성 중" 사라질 때까지 기다림 (생성 완료 확인)
        logger.info("[CUA] Phase 3b-2: '보고서 생성 중' 사라질 때까지 대기 (최대 %dms)", PHASE3B_WAIT_MS)
        try:
            page.wait_for_function(
                "() => !document.body.innerText.includes('보고서 생성 중')",
                timeout=PHASE3B_WAIT_MS,
            )
            logger.info("[CUA] Phase 3b-2 완료: '보고서 생성 중' 사라짐")
        except Exception as e:
            logger.warning("[CUA] Phase 3b-2 타임아웃 (%dms): %s — 강제 추출 시도", PHASE3B_WAIT_MS, e)

        time.sleep(3)  # 렌더링 안정화

        # 스튜디오 보고서 폴링 (최대 120초, 5초 간격)
        # _extract_studio_report는 생성 중이면 "" 반환 → 실제 내용이 나올 때까지 재시도
        report_text = ""
        for attempt in range(24):
            report_text = _extract_studio_report(page)
            if report_text:
                logger.info("[CUA] 스튜디오 보고서 확인됨 (시도 %d/%d)", attempt + 1, 24)
                break
            logger.info("[CUA] 추출 재시도 %d/24 — 스튜디오 콘텐츠 대기 중...", attempt + 1)
            time.sleep(5)

        if not report_text:
            logger.warning("[CUA] 스튜디오 폴링 실패 — CSS 셀렉터 폴백 시도")
            report_text = _extract_report_from_dom(page)

        context.close()

        if not report_text:
            raise RuntimeError("보고서 DOM 추출 결과 없음")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(report_text, encoding="utf-8")
        logger.info("[CUA] 보고서 저장 완료: %s (%d chars)", output_path, len(report_text))
        return output_path


def main():
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
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=args.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            titles = list_reports(page, args.notebook_url)
            context.close()

        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
        print(f"✅ {len(titles)} reports listed → {args.output}")

    elif args.mode == "get":
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=args.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            result = get_existing_report(page, args.notebook_url, args.report_index, args.output)
            context.close()
        print(f"✅ {result}")


if __name__ == "__main__":
    main()
