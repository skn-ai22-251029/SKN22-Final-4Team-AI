import base64
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel
from pydantic_settings import BaseSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────

class Settings(BaseSettings):
    gateway_internal_secret: str
    notebooklm_default_notebook_id: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()

SCRIPTS_DIR = Path(os.getenv("NOTEBOOKLM_SCRIPTS_DIR", "/app/scripts"))
DATA_DIR = Path(os.getenv("NOTEBOOKLM_DATA_DIR", "/app/data"))
REPORTS_DIR = DATA_DIR / "reports"
LIBRARY_JSON = DATA_DIR / "library.json"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_executor = ThreadPoolExecutor(max_workers=2)

app = FastAPI(title="NotebookLM Service")


# ─────────────────────────────────────────
# 모델
# ─────────────────────────────────────────

class GenerateRequest(BaseModel):
    job_id: str
    prompt: str
    notebook_id: Optional[str] = None
    notebook_url: Optional[str] = None


class GenerateResponse(BaseModel):
    status: str  # "success" or "error"
    report_content: Optional[str] = None
    file_content_b64: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None


class ListReportsRequest(BaseModel):
    notebook_id: Optional[str] = None
    notebook_url: Optional[str] = None
    channel_id: Optional[str] = None


class ListReportsResponse(BaseModel):
    status: str
    reports: list[str] = []
    error: Optional[str] = None


class GetReportRequest(BaseModel):
    job_id: str
    notebook_id: Optional[str] = None
    notebook_url: Optional[str] = None
    channel_id: Optional[str] = None
    report_index: int


class AddSourceRequest(BaseModel):
    source_url: str
    source_title: str = ""
    notebook_id: Optional[str] = None
    notebook_url: Optional[str] = None
    channel_id: str = ""              # YouTube 채널 ID로 노트북 자동 조회
    channel_name: str = ""            # CUA 폴백용 채널 표시 이름
    max_sources: int = 20             # 슬라이딩 윈도우 한도


class AddSourceResponse(BaseModel):
    status: str
    added: bool = False
    duplicate: bool = False
    cleaned_up: int = 0
    error: Optional[str] = None


class CreateNotebookRequest(BaseModel):
    name: str                          # 노트북 표시 이름
    channel_id: str                    # YouTube 채널 ID (예: "UCUpJs89fSBXNolQGOYKn0YQ")
    channel_name: str = ""             # 채널 표시 이름 (예: "노마드코더")


class CreateNotebookResponse(BaseModel):
    status: str
    notebook_id: str = ""
    notebook_url: str = ""
    error: Optional[str] = None


class AllChannelsResponse(BaseModel):
    status: str
    channels: list[dict] = []  # [{"id": "UCxxx", "name": "노마드코더"}, ...]


# ─────────────────────────────────────────
# 인증
# ─────────────────────────────────────────

def verify_secret(x_internal_secret: Optional[str] = None) -> None:
    if x_internal_secret != settings.gateway_internal_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Internal-Secret header",
        )


# ─────────────────────────────────────────
# 노트북 URL 결정
# ─────────────────────────────────────────

def _get_notebook_url(channel_id: str) -> Optional[str]:
    """channel_id → notebook_url 직접 조회. 쿼리 파라미터는 제거해서 반환."""
    try:
        if not LIBRARY_JSON.exists():
            return None
        lib = json.loads(LIBRARY_JSON.read_text())
        ch = lib.get("channels", {}).get(channel_id)
        if ch and ch.get("notebook_url"):
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(ch["notebook_url"])
            clean_url = urlunparse(parsed._replace(query="", fragment=""))
            logger.info("[resolve] channel_id=%s → %s", channel_id, clean_url)
            return clean_url
        logger.warning("[resolve] channel_id=%r 에 해당하는 노트북 없음", channel_id)
    except Exception as e:
        logger.warning("library.json 읽기 실패: %s", e)
    return None


# ─────────────────────────────────────────
# subprocess 실행 (blocking)
# ─────────────────────────────────────────

def _run_generate_report(
    job_id: str,
    prompt: str,
    notebook_url: Optional[str],
    output_path: Path,
) -> GenerateResponse:
    if not notebook_url:
        return GenerateResponse(status="error", error="notebook_url이 필요합니다.")

    cmd = [
        "python3",
        str(SCRIPTS_DIR / "generate_report_cua.py"),
        "--prompt", prompt,
        "--notebook-url", notebook_url,
        "--output", str(output_path),
        "--headless",
    ]

    logger.info("[notebooklm] starting subprocess job_id=%s cmd=%s", job_id, cmd)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=540,
        )
    except subprocess.TimeoutExpired:
        logger.error("[notebooklm] subprocess timeout job_id=%s", job_id)
        return GenerateResponse(status="error", error="subprocess timeout (540s)")
    except Exception as e:
        logger.error("[notebooklm] subprocess error job_id=%s: %s", job_id, e)
        return GenerateResponse(status="error", error=str(e))

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    logger.info("[notebooklm] subprocess done job_id=%s returncode=%d", job_id, result.returncode)

    # 서브프로세스 로그를 항상 출력 (로그인/CUA 흐름 추적용)
    if stdout.strip():
        for line in stdout.strip().splitlines():
            logger.info("[script] %s", line)
    if stderr.strip():
        for line in stderr.strip().splitlines():
            logger.warning("[script:err] %s", line)

    if result.returncode != 0:
        error_msg = stderr.strip() or stdout.strip() or f"returncode={result.returncode}"
        logger.error("[notebooklm] generate_report failed job_id=%s: %s", job_id, error_msg)
        return GenerateResponse(status="error", error=error_msg[:500])

    # 출력 파일 읽기
    if not output_path.exists():
        logger.error("[notebooklm] output file not found job_id=%s path=%s", job_id, output_path)
        return GenerateResponse(status="error", error=f"output file not found: {output_path}")

    report_content = output_path.read_text(encoding="utf-8")
    file_bytes = output_path.read_bytes()
    file_b64 = base64.b64encode(file_bytes).decode("utf-8")

    logger.info("[notebooklm] report ready job_id=%s size=%d chars", job_id, len(report_content))
    return GenerateResponse(
        status="success",
        report_content=report_content,
        file_content_b64=file_b64,
        filename=output_path.name,
    )


def _run_list_reports(notebook_url: str) -> ListReportsResponse:
    """subprocess로 --mode list 실행 → 보고서 제목 목록 반환."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    cmd = [
        "python3",
        str(SCRIPTS_DIR / "generate_report_cua.py"),
        "--mode", "list",
        "--notebook-url", notebook_url,
        "--output", output_path,
        "--headless",
    ]
    logger.info("[notebooklm] list_reports subprocess: %s", cmd)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ListReportsResponse(status="error", error="list-reports timeout (120s)")
    except Exception as e:
        return ListReportsResponse(status="error", error=str(e))

    for line in (result.stdout or "").strip().splitlines():
        logger.info("[script] %s", line)
    for line in (result.stderr or "").strip().splitlines():
        logger.warning("[script:err] %s", line)

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:300]
        return ListReportsResponse(status="error", error=err)

    try:
        titles = json.loads(Path(output_path).read_text(encoding="utf-8"))
        return ListReportsResponse(status="success", reports=titles)
    except Exception as e:
        return ListReportsResponse(status="error", error=f"JSON 파싱 실패: {e}")
    finally:
        Path(output_path).unlink(missing_ok=True)


def _run_get_report(
    job_id: str,
    notebook_url: str,
    report_index: int,
    output_path: Path,
) -> GenerateResponse:
    """subprocess로 --mode get 실행 → 기존 보고서 추출."""
    cmd = [
        "python3",
        str(SCRIPTS_DIR / "generate_report_cua.py"),
        "--mode", "get",
        "--notebook-url", notebook_url,
        "--report-index", str(report_index),
        "--output", str(output_path),
        "--headless",
    ]
    logger.info("[notebooklm] get_report subprocess job_id=%s: %s", job_id, cmd)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
    except subprocess.TimeoutExpired:
        return GenerateResponse(status="error", error="get-report timeout (360s)")
    except Exception as e:
        return GenerateResponse(status="error", error=str(e))

    for line in (result.stdout or "").strip().splitlines():
        logger.info("[script] %s", line)
    for line in (result.stderr or "").strip().splitlines():
        logger.warning("[script:err] %s", line)

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:500]
        return GenerateResponse(status="error", error=err)

    if not output_path.exists():
        return GenerateResponse(status="error", error=f"output file not found: {output_path}")

    report_content = output_path.read_text(encoding="utf-8")
    file_b64 = base64.b64encode(output_path.read_bytes()).decode("utf-8")
    logger.info("[notebooklm] get_report ready job_id=%s size=%d chars", job_id, len(report_content))
    return GenerateResponse(
        status="success",
        report_content=report_content,
        file_content_b64=file_b64,
        filename=output_path.name,
    )


# ─────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────

@app.post("/generate", response_model=GenerateResponse)
async def generate(
    body: GenerateRequest,
    x_internal_secret: Optional[str] = Header(default=None),
) -> GenerateResponse:
    """NotebookLM 보고서 생성. 실패 시에도 HTTP 200 + status="error" 반환."""
    verify_secret(x_internal_secret)

    notebook_url = body.notebook_url
    if not notebook_url:
        logger.error("[notebooklm] no notebook_url resolved job_id=%s", body.job_id)
        return GenerateResponse(
            status="error",
            error="notebook_url이 필요합니다.",
        )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"report_{body.job_id[:8]}_{timestamp}.md"
    output_path = REPORTS_DIR / output_filename

    import asyncio
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        _executor,
        _run_generate_report,
        body.job_id,
        body.prompt,
        notebook_url,
        output_path,
    )
    return response


@app.post("/list-reports", response_model=ListReportsResponse)
async def list_reports_endpoint(
    body: ListReportsRequest,
    x_internal_secret: Optional[str] = Header(default=None),
) -> ListReportsResponse:
    """NotebookLM 스튜디오에서 기존 보고서 목록을 조회한다."""
    verify_secret(x_internal_secret)

    notebook_url = body.notebook_url or (
        _get_notebook_url(body.channel_id) if body.channel_id else None
    )
    if not notebook_url:
        return ListReportsResponse(status="error", error="notebook_url을 결정할 수 없습니다.")

    import asyncio
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(_executor, _run_list_reports, notebook_url)
    return response


@app.post("/get-report", response_model=GenerateResponse)
async def get_report_endpoint(
    body: GetReportRequest,
    x_internal_secret: Optional[str] = Header(default=None),
) -> GenerateResponse:
    """기존 보고서 타일을 클릭해서 내용을 추출한다."""
    verify_secret(x_internal_secret)

    notebook_url = body.notebook_url or (
        _get_notebook_url(body.channel_id) if body.channel_id else None
    )
    if not notebook_url:
        return GenerateResponse(status="error", error="notebook_url을 결정할 수 없습니다.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"report_{body.job_id[:8]}_{timestamp}_existing.md"
    output_path = REPORTS_DIR / output_filename

    import asyncio
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        _executor,
        _run_get_report,
        body.job_id,
        notebook_url,
        body.report_index,
        output_path,
    )
    return response


def _save_notebook_url_to_library(channel_id: str, channel_name: str, notebook_url: str) -> None:
    """library.json의 channels[channel_id]에 notebook_url + name을 저장 (history는 건드리지 않음)."""
    try:
        lib: dict = {}
        if LIBRARY_JSON.exists():
            lib = json.loads(LIBRARY_JSON.read_text(encoding="utf-8"))
        ch = lib.setdefault("channels", {}).setdefault(channel_id, {})
        ch["notebook_url"] = notebook_url
        if channel_name:
            ch["name"] = channel_name
        LIBRARY_JSON.parent.mkdir(parents=True, exist_ok=True)
        LIBRARY_JSON.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[library] CUA 폴백 저장: channel_id=%s → %s", channel_id, notebook_url)
    except Exception as e:
        logger.warning("[library] 저장 실패: %s", e)


def _get_notebook_url_via_cua(channel_name: str, channel_id: str) -> Optional[str]:
    """manage_sources_cua.py --mode find로 NotebookLM 홈에서 노트북 URL을 찾는다."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    cmd = [
        "python3",
        str(SCRIPTS_DIR / "manage_sources_cua.py"),
        "--mode", "find",
        "--channel-name", channel_name,
        "--output", output_path,
        "--headless",
    ]
    logger.info("[cua-fallback] FIND_NB 시작: channel_name=%r", channel_name)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        logger.error("[cua-fallback] FIND_NB timeout (180s)")
        return None
    except Exception as e:
        logger.error("[cua-fallback] FIND_NB error: %s", e)
        return None

    for line in (result.stdout or "").strip().splitlines():
        logger.info("[script] %s", line)
    for line in (result.stderr or "").strip().splitlines():
        logger.warning("[script:err] %s", line)

    try:
        data = json.loads(Path(output_path).read_text(encoding="utf-8"))
        url = data.get("notebook_url", "")
        if url:
            _save_notebook_url_to_library(channel_id, channel_name, url)
        return url or None
    except Exception as e:
        logger.warning("[cua-fallback] 결과 파싱 실패: %s", e)
        return None
    finally:
        Path(output_path).unlink(missing_ok=True)


def _run_check_and_add_source(
    source_url: str,
    source_title: str,
    notebook_url: str,
    max_sources: int,
    channel_id: str = "",
    channel_name: str = "",
) -> AddSourceResponse:
    """소스 추가 + 슬라이딩 윈도우 정리를 subprocess로 실행."""
    scripts_dir = SCRIPTS_DIR
    manage_script = scripts_dir / "manage_sources_cua.py"

    # Step 0: notebook_url 미결정 시 CUA 폴백
    if not notebook_url:
        if not channel_name:
            return AddSourceResponse(status="error", error="notebook_url도 channel_name도 없음")
        notebook_url = _get_notebook_url_via_cua(channel_name, channel_id) or ""
        if not notebook_url:
            return AddSourceResponse(
                status="error",
                error=f"CUA fallback 실패: channel_name={channel_name!r}",
            )

    # Step 1: 중복 확인 (sources_log.json 직접 읽기)
    sources_log_path = DATA_DIR / "sources_log.json"
    try:
        if sources_log_path.exists():
            log = json.loads(sources_log_path.read_text(encoding="utf-8"))
            if any(
                s["url"] == source_url and s.get("notebook_url") == notebook_url
                for s in log.get("sources", [])
            ):
                logger.info("[add-source] 중복 건너뜀: %s", source_url)
                return AddSourceResponse(status="ok", duplicate=True)
    except Exception as e:
        logger.warning("[add-source] 중복 체크 실패: %s", e)

    # Step 2: 소스 추가
    add_cmd = [
        "python3", str(manage_script),
        "--mode", "add",
        "--notebook-url", notebook_url,
        "--source-url", source_url,
        "--source-title", source_title or source_url[:80],
        "--headless",
    ]
    logger.info("[add-source] subprocess 시작: %s", source_url)
    try:
        result = subprocess.run(add_cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return AddSourceResponse(status="error", error="add-source timeout (300s)")
    except Exception as e:
        return AddSourceResponse(status="error", error=str(e))

    for line in (result.stdout or "").strip().splitlines():
        logger.info("[script] %s", line)
    for line in (result.stderr or "").strip().splitlines():
        logger.warning("[script:err] %s", line)

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:400]
        return AddSourceResponse(status="error", error=err)

    # Step 3: 슬라이딩 윈도우 정리
    cleaned_up = 0
    cleanup_cmd = [
        "python3", str(manage_script),
        "--mode", "cleanup",
        "--notebook-url", notebook_url,
        "--max-sources", str(max_sources),
        "--headless",
    ]
    try:
        cr = subprocess.run(cleanup_cmd, capture_output=True, text=True, timeout=600)
        for line in (cr.stdout or "").strip().splitlines():
            logger.info("[script] %s", line)
        # 삭제 개수 파싱
        import re
        m = re.search(r"(\d+) sources removed", cr.stdout or "")
        if m:
            cleaned_up = int(m.group(1))
    except Exception as e:
        logger.warning("[add-source] cleanup 실패 (무시): %s", e)

    logger.info("[add-source] 완료: %s (cleaned=%d)", source_url, cleaned_up)
    return AddSourceResponse(status="ok", added=True, cleaned_up=cleaned_up)


def _run_create_notebook(name: str, channel_id: str, channel_name: str) -> CreateNotebookResponse:
    """subprocess로 create_notebook_cua.py 실행."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    cmd = [
        "python3",
        str(SCRIPTS_DIR / "create_notebook_cua.py"),
        "--name", name,
        "--channel-id", channel_id,
        "--channel-name", channel_name,
        "--output", output_path,
        "--headless",
    ]
    logger.info("[create-notebook] subprocess 시작: name=%r channel_id=%r", name, channel_id)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return CreateNotebookResponse(status="error", error="create-notebook timeout (300s)")
    except Exception as e:
        return CreateNotebookResponse(status="error", error=str(e))

    for line in (result.stdout or "").strip().splitlines():
        logger.info("[script] %s", line)
    for line in (result.stderr or "").strip().splitlines():
        logger.warning("[script:err] %s", line)

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:400]
        return CreateNotebookResponse(status="error", error=err)

    try:
        data = json.loads(Path(output_path).read_text(encoding="utf-8"))
        return CreateNotebookResponse(
            status="success",
            notebook_url=data["notebook_url"],
        )
    except Exception as e:
        return CreateNotebookResponse(status="error", error=f"결과 파싱 실패: {e}")
    finally:
        Path(output_path).unlink(missing_ok=True)


@app.post("/create-notebook", response_model=CreateNotebookResponse)
async def create_notebook_endpoint(
    body: CreateNotebookRequest,
    x_internal_secret: Optional[str] = Header(default=None),
) -> CreateNotebookResponse:
    """새 NotebookLM 노트북을 생성하고 library.json에 등록한다."""
    verify_secret(x_internal_secret)

    import asyncio
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        _executor,
        _run_create_notebook,
        body.name,
        body.channel_id,
        body.channel_name,
    )
    return response


@app.post("/check-and-add-source", response_model=AddSourceResponse)
async def check_and_add_source(
    body: AddSourceRequest,
    x_internal_secret: Optional[str] = Header(default=None),
) -> AddSourceResponse:
    """YouTube URL 등을 NotebookLM 소스로 추가. 중복 체크 + 슬라이딩 윈도우 정리 포함."""
    verify_secret(x_internal_secret)

    notebook_url = body.notebook_url or (
        _get_notebook_url(body.channel_id) if body.channel_id else None
    ) or ""

    import asyncio
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        _executor,
        _run_check_and_add_source,
        body.source_url,
        body.source_title,
        notebook_url,
        body.max_sources,
        body.channel_id,
        body.channel_name,
    )
    return response


@app.get("/all-channels", response_model=AllChannelsResponse)
async def all_channels_endpoint(
    x_internal_secret: Optional[str] = Header(default=None),
) -> AllChannelsResponse:
    """library.json에 등록된 모든 채널 목록을 반환한다."""
    verify_secret(x_internal_secret)
    if not LIBRARY_JSON.exists():
        return AllChannelsResponse(status="success", channels=[])
    try:
        lib = json.loads(LIBRARY_JSON.read_text(encoding="utf-8"))
        channels = [
            {"id": k, "name": v.get("name", k)}
            for k, v in lib.get("channels", {}).items()
        ]
        return AllChannelsResponse(status="success", channels=channels)
    except Exception as e:
        logger.warning("[all-channels] library.json 읽기 실패: %s", e)
        return AllChannelsResponse(status="success", channels=[])


@app.get("/health")
async def health(
    x_internal_secret: Optional[str] = Header(default=None),
) -> dict:
    auth_ok = x_internal_secret == settings.gateway_internal_secret

    active_notebook = None
    try:
        if LIBRARY_JSON.exists():
            with LIBRARY_JSON.open() as f:
                lib = json.load(f)
            active_id = lib.get("active_notebook_id")
            if active_id:
                nb = lib.get("notebooks", {}).get(active_id)
                active_notebook = nb.get("name") if nb else active_id
    except Exception:
        pass

    return {
        "status": "ok",
        "auth_ok": auth_ok,
        "active_notebook": active_notebook,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
