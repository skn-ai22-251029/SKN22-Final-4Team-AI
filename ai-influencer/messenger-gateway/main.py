import asyncio
import logging
import base64
import json
import re
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Awaitable, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status

from adapters.discord_adapter import DiscordAdapter
from config import settings
from utils.file_naming import build_filename
from services.storage_service import presign_s3_uri, put_bytes_and_presign
from models.job import (
    AutoReportRequest,
    ChannelSelectRequest,
    ConfirmActionRequest,
    IncomingMessageRequest,
    ListJobsRequest,
    ManualGenerateRequest,
    MessengerSource,
    ReportMessageRequest,
    ReportSelectRequest,
    ReportToVideoRequest,
    SendAudioRequest,
    SendConfirmRequest,
    SendReportRequest,
    SendTextRequest,
    SendVideoPreviewRequest,
    TtsActionRequest,
    VideoActionRequest,
)
from services import job_service, n8n_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 싱글턴 어댑터 및 httpx 클라이언트
_http_client: Optional[httpx.AsyncClient] = None
_discord_adapter: Optional[DiscordAdapter] = None
_DISCORD_ATTACHMENT_LIMIT_BYTES = 10 * 1024 * 1024

_REPORT_SYSTEM_PROMPT = (
    "[중요] 대사 외 다른 표기는 절대 넣지 않는다.(예시: \"[오프닝]\") "
    "대본의 제목도 넣지 않는다. [내용]에 대한 대사만 작성한다. "
    "\"?, !, ., ,\" 글쓰기에 필요한 기호만 사용한다. "
    "기호를 적절히 사용해서 TTS가 읽을 때 자연스럽게 이어지는 억양을 준다(물음표는 올리는 악센트, 마침표는 쉬어가는 악센트, 쉼표는 문장이 길어서 정말 필요할 때만 사용한다.) "
    "[제약사항] 반드시 한글만으로 이루어져야 한다. "
    "영어 사용 금지(예시: \"AI\" -> \"에이아이\") "
    "숫자도 한글로 표기할 것. "
    "마크다운 문법 사용하지 않고 텍스트만으로 작성한다. "
    "[형식] 50초 분량의 짧은 영상의 대사(약 300자). "
    "반드시 하리의 컨셉이 유지되어야 한다. "
    "대사만 포함되어야 한다. "
    "인삿말(오프닝) - 본문 - 마무리(엔딩) 구조로 진행한다. "
    "[내용] "
)


def _extract_valid_basename(job_id: str, filename: object) -> Optional[str]:
    if not isinstance(filename, str) or not filename:
        return None
    pattern = rf"^(\d{{8}}-{re.escape(job_id)})\.(txt|wav|mp4)$"
    match = re.match(pattern, filename)
    if not match:
        return None
    return match.group(1)


def _resolve_media_basename(
    job_id: str,
    existing_script_json: object = None,
    candidate_filename: Optional[str] = None,
) -> str:
    parsed = _as_script_json(existing_script_json)
    media_names = parsed.get("media_names")
    if isinstance(media_names, dict):
        for key in ("report_filename", "audio_filename", "video_filename"):
            candidate = media_names.get(key)
            basename = _extract_valid_basename(job_id, candidate)
            if basename:
                return basename
    candidate_basename = _extract_valid_basename(job_id, candidate_filename)
    if candidate_basename:
        return candidate_basename
    return build_filename(job_id, "txt").rsplit(".", 1)[0]


def _normalize_filename(
    job_id: str,
    ext: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    normalized = f"{_resolve_media_basename(job_id, existing_script_json, candidate)}.{ext}"
    if candidate and candidate != normalized:
        logger.info("[file-naming] normalize %s filename job_id=%s from=%s to=%s", ext, job_id, candidate, normalized)
    return normalized


def _normalize_report_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "txt", candidate, existing_script_json)


def _normalize_audio_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "wav", candidate, existing_script_json)


def _normalize_video_filename(
    job_id: str,
    candidate: Optional[str],
    existing_script_json: object = None,
) -> str:
    return _normalize_filename(job_id, "mp4", candidate, existing_script_json)


def _as_script_json(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return dict(parsed)
        except json.JSONDecodeError:
            return {}
    return {}


def _merge_script_json_with_media_names(
    existing: object,
    *,
    report_filename: Optional[str] = None,
    audio_filename: Optional[str] = None,
    video_filename: Optional[str] = None,
    script_text: Optional[str] = None,
) -> dict:
    merged = _as_script_json(existing)
    if script_text is not None:
        merged["script_text"] = script_text
        merged["script"] = script_text  # backward compatibility
        merged["script_summary"] = script_text[:200]
    media_names = merged.get("media_names")
    if not isinstance(media_names, dict):
        media_names = {}
    if report_filename:
        media_names["report_filename"] = report_filename
    if audio_filename:
        media_names["audio_filename"] = audio_filename
    if video_filename:
        media_names["video_filename"] = video_filename
    if media_names:
        merged["media_names"] = media_names
    return merged


def _discord_attachment_limit_bytes() -> int:
    limit = settings.media_max_discord_file_bytes
    if limit <= 0:
        return _DISCORD_ATTACHMENT_LIMIT_BYTES
    return limit


def _normalize_manual_job_id(job_id: str) -> str:
    return (job_id or "").strip()


def _format_job_summary(job: dict) -> str:
    job_id = job.get("id", "")
    status = job.get("status", "")
    return f"{job_id[:8]}  status={status}"


def _to_iso8601(value: object) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[attr-defined]
    return str(value)


async def _resolve_manual_job(
    body: ManualGenerateRequest,
    *,
    require_script: bool = False,
    require_audio: bool = False,
) -> dict:
    user_id = (body.messenger_user_id or "").strip()
    channel_id = (body.messenger_channel_id or "").strip()
    normalized_job_id = _normalize_manual_job_id(body.job_id)
    if not user_id or not channel_id:
        if not normalized_job_id:
            raise HTTPException(
                status_code=400,
                detail="job_id is required when messenger_user_id/channel_id are missing",
            )
        exact = await job_service.get_job(normalized_job_id)
        if exact is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return exact

    if not normalized_job_id:
        latest = await job_service.get_latest_job(
            user_id,
            channel_id,
            require_script=require_script,
            require_audio=require_audio,
        )
        if latest is None:
            requirement = "script_text" if require_script else "audio_url" if require_audio else "조건"
            raise HTTPException(
                status_code=404,
                detail=f"No recent jobs found for this user/channel with required {requirement}",
            )
        resolved_job = await job_service.get_job(latest["id"])
        if resolved_job is None:
            raise HTTPException(status_code=404, detail="Resolved latest job no longer exists")
        return resolved_job

    exact = await job_service.get_job(normalized_job_id)
    if exact is not None:
        if exact.get("messenger_user_id") != user_id or exact.get("messenger_channel_id") != channel_id:
            raise HTTPException(status_code=403, detail="Job belongs to a different user/channel")
        return exact

    matches = await job_service.find_jobs_by_prefix(
        normalized_job_id,
        user_id,
        channel_id,
        require_script=require_script,
        require_audio=require_audio,
    )
    if len(matches) == 1:
        resolved_job = await job_service.get_job(matches[0]["id"])
        if resolved_job is None:
            raise HTTPException(status_code=404, detail="Resolved job no longer exists")
        return resolved_job

    if len(matches) > 1:
        items = ", ".join(_format_job_summary(m) for m in matches[:5])
        raise HTTPException(
            status_code=409,
            detail=f"Ambiguous job_id prefix. Matches: {items}",
        )

    raise HTTPException(status_code=404, detail="Job not found for this user/channel")


def _parse_topic_channels(raw: str) -> list[dict]:
    """TOPIC_CHANNELS(채널명/채널ID+...)를 버튼용 채널 목록으로 파싱."""
    channels: list[dict] = []
    seen: set[str] = set()
    for chunk in (raw or "").split("+"):
        part = chunk.strip()
        if not part:
            continue
        slash_idx = part.find("/")
        if slash_idx < 0:
            continue
        name = part[:slash_idx].strip()
        cid = part[slash_idx + 1 :].strip()
        if not name or not cid:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        channels.append({"id": cid, "name": name})
    return channels


def _parse_csv_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _get_primary_discord_channel_id() -> str:
    channel_ids = _parse_csv_ids(settings.discord_allowed_channel_ids)
    if not channel_ids:
        raise HTTPException(
            status_code=400,
            detail="DISCORD_ALLOWED_CHANNEL_IDS is empty; cannot route auto report",
        )
    return channel_ids[0]


def _build_auto_report_prompt(body: AutoReportRequest) -> str:
    segments: list[str] = []
    if body.channel_name:
        segments.append(f"대상 채널명: {body.channel_name}.")
    if body.source_title:
        segments.append(f"최신 참고 영상 제목: {body.source_title}.")
    if body.source_url:
        segments.append(f"최신 참고 영상 URL: {body.source_url}.")
    segments.append("위 최신 소스를 참고해 오늘 업로드 흐름에 맞는 대사를 작성한다.")
    return _REPORT_SYSTEM_PROMPT + " ".join(segments)


def _launch_bg_task(coro: Awaitable[None], *, task_name: str, job_id: str) -> None:
    task = asyncio.create_task(coro)

    def _done_callback(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except Exception:
            logger.exception("[%s] background task failed job_id=%s", task_name, job_id)

    task.add_done_callback(_done_callback)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _discord_adapter

    await job_service.get_db_pool()

    _http_client = httpx.AsyncClient(timeout=10.0)
    _discord_adapter = DiscordAdapter(token=settings.discord_bot_token, http_client=_http_client)

    logger.info("messenger-gateway started (discord adapter ready)")
    yield

    await job_service.close_db_pool()
    await n8n_service.close_http_client()
    if _http_client:
        await _http_client.aclose()
    logger.info("messenger-gateway shutdown")


app = FastAPI(title="Messenger Gateway", lifespan=lifespan)


# ─────────────────────────────────────────
# 인증 의존성
# ─────────────────────────────────────────

async def verify_secret(x_internal_secret: Annotated[Optional[str], Header()] = None) -> None:
    if x_internal_secret != settings.gateway_internal_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Internal-Secret header",
        )


AuthDep = Annotated[None, Depends(verify_secret)]


# ─────────────────────────────────────────
# 어댑터 반환 헬퍼
# ─────────────────────────────────────────

def get_adapter(messenger_source: str) -> DiscordAdapter:
    if messenger_source == "discord":
        return _discord_adapter
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown messenger_source: {messenger_source}",
    )


# ─────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────

@app.post("/internal/message")
async def receive_message(_: AuthDep, body: IncomingMessageRequest) -> dict:
    """봇 서비스가 포워딩하는 사용자 메시지를 수신한다."""
    await job_service.create_job(body)

    try:
        await n8n_service.call_wf01_input(
            job_id=body.job_id,
            messenger_source=body.messenger_source.value,
            messenger_user_id=body.messenger_user_id,
            messenger_channel_id=body.messenger_channel_id,
            concept_text=body.concept_text,
            ref_image_url=body.ref_image_url,
            character_id=body.character_id,
        )
    except Exception as e:
        logger.error("[discord] call_wf01_input failed job_id=%s: %s", body.job_id, e)
        await job_service.update_job(body.job_id, error_message=str(e))

    return {"job_id": body.job_id, "status": "accepted"}


@app.post("/internal/send-confirm")
async def send_confirm(_: AuthDep, body: SendConfirmRequest) -> dict:
    """n8n WF-04에서 호출 — Discord로 컨펌 메시지를 전송한다."""
    try:
        confirm_message_id = await _discord_adapter.send_confirm_message(
            channel_id=body.messenger_channel_id,
            user_id=body.messenger_user_id,
            job_id=body.job_id,
            title=body.title,
            script_summary=body.script_summary,
            preview_url=body.preview_url,
        )
    except Exception as e:
        logger.error("[discord] send_confirm_message failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    await job_service.update_job(body.job_id, confirm_message_id=confirm_message_id)
    await job_service.transition_status(body.job_id, "WAITING_APPROVAL")

    logger.info("[discord] send_confirm done job_id=%s", body.job_id)
    return {"job_id": body.job_id, "confirm_message_id": confirm_message_id}


@app.post("/internal/confirm-action")
async def confirm_action(_: AuthDep, body: ConfirmActionRequest) -> dict:
    """봇 서비스가 버튼 클릭 이벤트를 포워딩한다."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] in ("APPROVED", "PUBLISHED", "PUBLISHING"):
        raise HTTPException(status_code=409, detail=f"Job already in status: {job['status']}")

    channel_id = job["messenger_channel_id"]
    confirm_message_id = job.get("confirm_message_id")

    if body.action == "approved":
        await job_service.transition_status(body.job_id, "APPROVED")

        if confirm_message_id:
            try:
                await _discord_adapter.remove_buttons(channel_id, confirm_message_id, "✅ 승인됨")
            except Exception as e:
                logger.error("[discord] remove_buttons failed job_id=%s: %s", body.job_id, e)

        try:
            await _discord_adapter.send_text_message(channel_id, "🎬 승인되었습니다! TTS 및 영상 생성을 시작합니다. (약 5~10분 소요)")
        except Exception as e:
            logger.error("[discord] send_text_message failed job_id=%s: %s", body.job_id, e)

        try:
            await n8n_service.call_wf05_confirm(body.job_id, "approved")
        except Exception as e:
            logger.error("call_wf05_confirm failed job_id=%s: %s", body.job_id, e)

        logger.info("[discord] confirm_action=approved job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "approved"}

    elif body.action == "revision_requested":
        if not body.revision_note:
            await job_service.update_job(body.job_id, status="REVISION_REQUESTED")
            logger.info("[discord] confirm_action=revision_requested (pending note) job_id=%s", body.job_id)
            return {"job_id": body.job_id, "action": "revision_requested", "pending_note": True}

        await job_service.transition_status(body.job_id, "REVISION_REQUESTED")

        current_revision_count = job.get("revision_count", 0) or 0
        await job_service.update_job(
            body.job_id,
            revision_note=body.revision_note,
            revision_count=current_revision_count + 1,
        )

        if confirm_message_id:
            try:
                await _discord_adapter.remove_buttons(channel_id, confirm_message_id, "✏️ 수정 요청됨")
            except Exception as e:
                logger.error("[discord] remove_buttons failed job_id=%s: %s", body.job_id, e)

        try:
            await _discord_adapter.send_text_message(channel_id, "🔄 수정 요청이 접수되었습니다. 재작업을 시작합니다.")
        except Exception as e:
            logger.error("[discord] send_text_message failed job_id=%s: %s", body.job_id, e)

        try:
            await n8n_service.call_wf05_confirm(body.job_id, "revision_requested", body.revision_note)
        except Exception as e:
            logger.error("call_wf05_confirm failed job_id=%s: %s", body.job_id, e)

        logger.info("[discord] confirm_action=revision_requested job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "revision_requested"}

    raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


@app.post("/internal/report-message")
async def report_message(_: AuthDep, body: ReportMessageRequest) -> dict:
    """봇 서비스가 report: 프리픽스 메시지를 포워딩한다."""
    await job_service.create_job(body)
    # 즉시 반환 후 background에서 list-reports → 버튼 or WF-06
    asyncio.create_task(_handle_report_message_bg(body))
    return {"job_id": body.job_id, "status": "accepted"}


@app.post("/internal/auto-report")
async def auto_report(_: AuthDep, body: AutoReportRequest) -> dict:
    """WF-09 소스 추가 성공 후 자동으로 WF-06 보고서 생성을 트리거한다."""
    target_channel_id = _get_primary_discord_channel_id()
    prompt = _build_auto_report_prompt(body)
    job_id = str(uuid.uuid4())

    report_body = ReportMessageRequest(
        job_id=job_id,
        messenger_source=MessengerSource.DISCORD,
        messenger_user_id="system:auto-report",
        messenger_channel_id=target_channel_id,
        prompt=prompt,
        notebook_id="",
        channel_id=body.channel_id,
        character_id="default-character",
    )

    await job_service.create_job(report_body)
    wf06_ok = await _call_wf06(report_body, raise_on_error=True)
    if not wf06_ok:
        raise HTTPException(status_code=500, detail="WF-06 trigger failed")
    logger.info(
        "[auto-report] triggered job_id=%s discord_channel=%s youtube_channel=%s source=%s",
        job_id,
        target_channel_id,
        body.channel_id,
        body.source_url,
    )
    return {
        "status": "triggered",
        "job_id": job_id,
        "messenger_channel_id": target_channel_id,
        "youtube_channel_id": body.channel_id,
    }


async def _get_all_channels() -> list[dict]:
    """채널 버튼 목록은 TOPIC_CHANNELS만 사용한다."""
    channels = _parse_topic_channels(settings.topic_channels)
    if not channels:
        logger.warning("[report-message] TOPIC_CHANNELS 파싱 결과가 비어 있음")
    return channels


async def _handle_report_message_bg(body: ReportMessageRequest) -> None:
    """채널 선택 버튼 표시."""
    channels = await _get_all_channels()
    if channels:
        try:
            await _discord_adapter.send_channel_list(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                channels=channels,
            )
        except Exception as e:
            logger.error("[report-message] send_channel_list failed job_id=%s: %s", body.job_id, e)
        return

    # 채널 없으면 WF-06 직행
    await _call_wf06(body)


async def _handle_channel_selected_bg(body: ReportMessageRequest) -> None:
    """채널 선택 후 → 기존 보고서 목록 조회 or WF-06."""
    logger.info(
        "[channel-select:bg] start job_id=%s channel_id=%s",
        body.job_id,
        body.channel_id,
    )
    try:
        await _discord_adapter.send_text_message(
            body.messenger_channel_id,
            "🔎 채널을 확인했습니다. 기존 보고서 목록을 조회 중입니다... (최대 3분)",
        )
    except Exception as e:
        logger.warning("[channel-select:bg] start notice failed job_id=%s: %s", body.job_id, e)

    reports: list[str] = []
    list_reports_error: str = ""
    list_reports_failed = False
    try:
        resp = await _http_client.post(
            f"{settings.notebooklm_service_url}/list-reports",
            json={
                "notebook_id": body.notebook_id or None,
                "channel_id": body.channel_id or None,
            },
            headers={"X-Internal-Secret": settings.gateway_internal_secret},
            timeout=180.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                reports = data.get("reports", [])
            else:
                list_reports_failed = True
                list_reports_error = str(data.get("error") or "status!=success")
                logger.warning(
                    "[channel-select:bg] list-reports status!=success job_id=%s error=%s",
                    body.job_id,
                    data.get("error"),
                )
        else:
            list_reports_failed = True
            list_reports_error = f"HTTP {resp.status_code}"
            logger.warning(
                "[channel-select:bg] list-reports non-200 job_id=%s status=%s body=%s",
                body.job_id,
                resp.status_code,
                (resp.text or "")[:300],
            )
    except Exception as e:
        list_reports_failed = True
        list_reports_error = str(e)
        logger.warning("[report-message] list-reports 조회 실패: %s", e)

    if reports:
        try:
            await _discord_adapter.send_report_list(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                reports=reports,
                selected_channel_id=body.channel_id,
            )
            logger.info("[channel-select:bg] reports listed job_id=%s count=%d", body.job_id, len(reports))
            return
        except Exception as e:
            logger.error("[report-message] send_report_list failed job_id=%s: %s", body.job_id, e)
            list_reports_failed = True
            list_reports_error = f"send_report_list failed: {e}"

    # 자동 fallback으로 바로 WF-06 실행하지 않고, 사용자 선택 버튼(다시 조회/새로 생성)을 제공한다.
    if list_reports_failed:
        reason_text = (
            "⚠️ 기존 보고서 목록 조회에 실패했습니다.\n"
            f"사유: {list_reports_error[:160]}\n"
            "아래에서 `다시 조회` 또는 `새로 생성`을 선택해주세요."
        )
        try:
            await _discord_adapter.send_report_recovery_actions(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                selected_channel_id=body.channel_id,
                reason_text=reason_text,
                include_retry=True,
            )
        except Exception as e:
            logger.warning("[channel-select:bg] recovery action send failed job_id=%s: %s", body.job_id, e)
        return

    # 정상적으로 성공 응답이지만 목록이 비어 있는 경우
    try:
        await _discord_adapter.send_report_recovery_actions(
            channel_id=body.messenger_channel_id,
            job_id=body.job_id,
            selected_channel_id=body.channel_id,
            reason_text=(
                "📄 해당 채널의 기존 보고서를 찾지 못했습니다.\n"
                "아래에서 `새로 생성`을 선택해 보고서를 만들 수 있습니다."
            ),
            include_retry=False,
        )
    except Exception as e:
        logger.warning("[channel-select:bg] empty-list action send failed job_id=%s: %s", body.job_id, e)


async def _call_wf06(body: ReportMessageRequest, *, raise_on_error: bool = False) -> bool:
    logger.info(
        "[report-wf06] trigger job_id=%s channel_id=%s messenger_channel=%s",
        body.job_id,
        body.channel_id,
        body.messenger_channel_id,
    )
    try:
        await n8n_service.call_wf06_report(
            job_id=body.job_id,
            messenger_source=body.messenger_source.value,
            messenger_user_id=body.messenger_user_id,
            messenger_channel_id=body.messenger_channel_id,
            prompt=body.prompt,
            notebook_id=body.notebook_id,
            channel_id=body.channel_id,
            character_id=body.character_id,
        )
        return True
    except Exception as e:
        logger.error("[discord] call_wf06_report failed job_id=%s: %s", body.job_id, e)
        await job_service.update_job(body.job_id, error_message=str(e))
        try:
            await _discord_adapter.send_text_message(
                body.messenger_channel_id,
                f"❌ 보고서 생성 요청 전송 실패(job `{body.job_id[:8]}`): {str(e)[:180]}",
            )
        except Exception:
            logger.warning("[report-wf06] failed to notify channel for wf06 error job_id=%s", body.job_id)
        if raise_on_error:
            raise
    return False


@app.post("/internal/channel-select")
async def channel_select(_: AuthDep, body: ChannelSelectRequest) -> dict:
    """Discord 채널 선택 버튼 클릭 → 해당 채널의 보고서 목록 조회."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    report_body = ReportMessageRequest(
        job_id=body.job_id,
        messenger_source=job["messenger_source"],
        messenger_user_id=job["messenger_user_id"],
        messenger_channel_id=job["messenger_channel_id"],
        prompt=job.get("concept_text", ""),
        notebook_id="",
        channel_id=body.channel_id,
        character_id=job.get("character_id", "default-character"),
    )
    _launch_bg_task(_handle_channel_selected_bg(report_body), task_name="channel-select", job_id=body.job_id)
    logger.info("[channel-select] job_id=%s channel_id=%s", body.job_id, body.channel_id)
    return {"job_id": body.job_id, "status": "accepted"}


@app.post("/internal/report-select")
async def report_select(_: AuthDep, body: ReportSelectRequest) -> dict:
    """Discord 보고서 선택/새로 생성 버튼 클릭을 처리한다."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if body.action not in ("select", "new"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")
    if body.action == "select" and body.report_index is None:
        raise HTTPException(status_code=400, detail="report_index is required for action=select")

    # 즉시 반환 후 background에서 처리 (get-report는 최대 300s 소요)
    _launch_bg_task(_handle_report_select_bg(body, job), task_name="report-select", job_id=body.job_id)
    return {"job_id": body.job_id, "status": "accepted"}


async def _handle_report_select_bg(body: ReportSelectRequest, job: dict) -> None:
    channel_id = job["messenger_channel_id"]

    if body.action == "select":
        # 진행 중 안내 메시지
        try:
            await _discord_adapter.send_text_message(channel_id, "📄 보고서를 가져오는 중입니다... (10~30초 소요)")
        except Exception:
            pass

        try:
            resp = await _http_client.post(
                f"{settings.notebooklm_service_url}/get-report",
                json={
                    "job_id": body.job_id,
                    "channel_id": body.channel_id or None,
                    "report_index": body.report_index,
                },
                headers={"X-Internal-Secret": settings.gateway_internal_secret},
                timeout=300.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("[report-select] get-report failed job_id=%s: %s", body.job_id, e)
            await _discord_adapter.send_text_message(channel_id, f"❌ 보고서 가져오기 실패: {e}")
            return

        if data.get("status") != "success":
            err = data.get("error", "get-report 실패")
            logger.error("[report-select] get-report error job_id=%s: %s", body.job_id, err)
            await _discord_adapter.send_text_message(channel_id, f"❌ 보고서 가져오기 실패: {err}")
            return

        report_content = data["report_content"]
        file_bytes = base64.b64decode(data["file_content_b64"])
        filename = _normalize_report_filename(body.job_id, data.get("filename"), job.get("script_json"))

        report_text = (report_content or "").strip()
        merged_script = _merge_script_json_with_media_names(
            job.get("script_json"),
            script_text=report_text if report_text else None,
            report_filename=filename,
        )
        await job_service.update_job(
            body.job_id,
            script_json=merged_script,
        )

        text = report_content
        if len(text) > 1800:
            text = text[:1800] + "\n\n[전체 내용은 첨부 파일 참조]"

        try:
            await _discord_adapter.send_file_message(
                channel_id=channel_id,
                text=text,
                file_bytes=file_bytes,
                filename=filename,
                include_video_button=True,
                job_id=body.job_id,
            )
        except Exception as e:
            logger.error("[report-select] send_file_message failed job_id=%s: %s", body.job_id, e)
            return

        await job_service.transition_status(body.job_id, "PUBLISHED")
        logger.info("[report-select] action=select done job_id=%s index=%d", body.job_id, body.report_index)

    elif body.action == "new":
        try:
            await _discord_adapter.send_text_message(channel_id, "🆕 새 보고서를 생성합니다... (최대 5분 소요)")
        except Exception:
            pass

        report_msg = ReportMessageRequest(
            job_id=job["id"],
            messenger_source=job["messenger_source"],
            messenger_user_id=job["messenger_user_id"],
            messenger_channel_id=channel_id,
            prompt=job.get("concept_text", ""),
            notebook_id="",
            channel_id=body.channel_id,
            character_id=job.get("character_id", "default-character"),
        )
        await _call_wf06(report_msg)
        logger.info("[report-select] action=new job_id=%s → WF-06 triggered", body.job_id)


@app.post("/internal/send-report")
async def send_report(_: AuthDep, body: SendReportRequest) -> dict:
    """n8n WF-06에서 호출 — Discord로 보고서 파일을 전송한다."""
    existing_job = await job_service.get_job(body.job_id)
    filename = _normalize_report_filename(
        body.job_id,
        body.filename,
        existing_job.get("script_json") if existing_job else None,
    )
    if body.file_content_b64:
        try:
            file_bytes = base64.b64decode(body.file_content_b64)
        except Exception as e:
            logger.error("[discord] base64 decode failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=400, detail=f"Invalid file_content_b64: {e}")
    else:
        file_bytes = (body.report_content or "").encode("utf-8")

    try:
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_reports,
            filename=filename,
            content=file_bytes,
            content_type="text/plain; charset=utf-8",
        )
    except Exception as e:
        logger.error("[storage] report upload failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=f"Report upload failed: {e}")

    is_link_only_report = stored.size_bytes > _discord_attachment_limit_bytes()
    text = body.report_content
    if len(text) > 1800:
        overflow_hint = (
            "[전체 내용은 아래 링크 참조]"
            if is_link_only_report
            else "[전체 내용은 첨부 파일 참조]"
        )
        text = text[:1800] + f"\n\n{overflow_hint}"

    report_text = (body.report_content or "").strip()
    merged_script = _merge_script_json_with_media_names(
        existing_job.get("script_json") if existing_job else None,
        script_text=report_text if report_text else None,
        report_filename=filename,
    )
    report_storage_url = stored.s3_uri
    await job_service.update_job(
        body.job_id,
        final_url=report_storage_url,
        script_json=merged_script,
    )

    if is_link_only_report:
        try:
            await _discord_adapter.send_report_link_message(
                channel_id=body.messenger_channel_id,
                text=text,
                report_url=stored.presigned_url,
                include_video_button=body.include_video_button,
                job_id=body.job_id,
            )
        except Exception as e:
            logger.error("[discord] send_report_link_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))
    else:
        try:
            await _discord_adapter.send_file_message(
                channel_id=body.messenger_channel_id,
                text=text,
                file_bytes=file_bytes,
                filename=filename,
                include_video_button=body.include_video_button,
                job_id=body.job_id,
            )
        except Exception as e:
            logger.error("[discord] send_file_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    logger.info("[discord] send_report done job_id=%s filename=%s", body.job_id, filename)
    return {
        "status": "sent",
        "filename": filename,
        "file_url": stored.presigned_url,
        "s3_uri": report_storage_url,
    }


@app.post("/internal/send-text")
async def send_text(_: AuthDep, body: SendTextRequest) -> dict:
    """n8n WF-05에서 특정 채널로 텍스트 메시지를 전송한다."""
    try:
        await _discord_adapter.send_text_message(body.messenger_channel_id, body.text)
    except Exception as e:
        logger.error("[discord] send_text failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "sent"}


@app.post("/internal/send-audio")
async def send_audio(_: AuthDep, body: SendAudioRequest) -> dict:
    """WF-11에서 호출 — Discord로 TTS 완료본(WAV + 승인/반려 버튼) 전송."""
    if not body.audio_content_b64:
        raise HTTPException(status_code=400, detail="audio_content_b64 is required")
    try:
        audio_bytes = base64.b64decode(body.audio_content_b64)
    except Exception as e:
        logger.error("[discord] audio base64 decode failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=400, detail=f"Invalid audio_content_b64: {e}")

    existing_job = await job_service.get_job(body.job_id)
    filename = _normalize_audio_filename(
        body.job_id,
        body.filename,
        existing_job.get("script_json") if existing_job else None,
    )
    try:
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_tts,
            filename=filename,
            content=audio_bytes,
            content_type="audio/wav",
        )
    except Exception as e:
        logger.error("[storage] tts upload failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=f"TTS upload failed: {e}")

    caption = body.caption or "🔊 TTS 완료본입니다. 승인하면 WF-12(HeyGen)로 진행합니다."
    if stored.size_bytes > _discord_attachment_limit_bytes():
        try:
            message_id = await _discord_adapter.send_tts_link_message(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                caption=caption,
                audio_url=stored.presigned_url,
                include_wf12_button=body.include_wf12_button,
            )
            attachment_url = stored.presigned_url
        except Exception as e:
            logger.error("[discord] send_tts_link_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))
    else:
        try:
            message_id, attachment_url = await _discord_adapter.send_tts_audio_message(
                channel_id=body.messenger_channel_id,
                job_id=body.job_id,
                caption=caption,
                audio_bytes=audio_bytes,
                filename=filename,
                include_wf12_button=body.include_wf12_button,
            )
        except Exception as e:
            logger.error("[discord] send_tts_audio_message failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    resolved_audio_url = stored.s3_uri
    merged_script = _merge_script_json_with_media_names(
        existing_job.get("script_json") if existing_job else None,
        audio_filename=filename,
    )
    await job_service.update_job(
        body.job_id,
        confirm_message_id=message_id,
        audio_url=resolved_audio_url,
        final_url=stored.s3_uri,
        script_json=merged_script,
    )
    logger.info("[discord] send_audio done job_id=%s filename=%s", body.job_id, filename)
    return {
        "status": "sent",
        "message_id": message_id,
        "attachment_url": attachment_url,
        "audio_url": stored.presigned_url,
        "filename": filename,
        "s3_uri": stored.s3_uri,
    }


@app.post("/internal/tts-action")
async def tts_action(_: AuthDep, body: TtsActionRequest) -> dict:
    """Discord TTS 완료본 승인/반려 버튼 처리."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]

    if body.action == "approve":
        audio_url = job.get("audio_url", "")
        if not audio_url:
            raise HTTPException(status_code=400, detail="No audio_url found in job")
        audio_file_path = ""
        approved_audio_url = audio_url
        if isinstance(audio_url, str) and audio_url.startswith("s3://"):
            try:
                approved_audio_url = presign_s3_uri(audio_url)
            except Exception as e:
                logger.error("[storage] presign audio s3 uri failed job_id=%s: %s", body.job_id, e)
                raise HTTPException(status_code=500, detail=f"audio presign failed: {e}")
        try:
            await n8n_service.call_wf12_heygen_generate(
                job_id=body.job_id,
                channel_id=channel_id,
                user_id=user_id,
                audio_file_path=audio_file_path,
                audio_url=approved_audio_url,
            )
            await _discord_adapter.send_text_message(channel_id, "🎬 WF-12(HeyGen) 영상 생성을 시작합니다.")
        except Exception as e:
            logger.error("call_wf12 (tts approve) failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))
        return {"job_id": body.job_id, "action": "approve"}

    if body.action == "reject":
        await job_service.transition_status(body.job_id, "APPROVED")
        await _discord_adapter.send_text_message(channel_id, "❌ TTS 반려됨. 필요 시 다시 TTS를 생성하세요.")
        return {"job_id": body.job_id, "action": "reject"}

    raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


@app.post("/internal/send-video-preview")
async def send_video_preview(_: AuthDep, body: SendVideoPreviewRequest) -> dict:
    """WF-12 완료 후 호출 — Discord로 영상 미리보기 + 승인/반려 버튼을 전송한다."""
    existing_job = await job_service.get_job(body.job_id)
    normalized_video_filename = _normalize_video_filename(
        body.job_id,
        body.video_filename,
        existing_job.get("script_json") if existing_job else None,
    )
    try:
        video_resp = await _http_client.get(body.video_url, timeout=300.0)
        video_resp.raise_for_status()
    except Exception as e:
        logger.error("[storage] video source download failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=f"Video download failed: {e}")

    try:
        stored = put_bytes_and_presign(
            prefix=settings.media_s3_prefix_videos,
            filename=normalized_video_filename,
            content=video_resp.content,
            content_type=video_resp.headers.get("content-type") or "video/mp4",
        )
    except Exception as e:
        logger.error("[storage] video upload failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=f"Video upload failed: {e}")

    try:
        message_id = await _discord_adapter.send_video_preview(
            channel_id=body.channel_id,
            user_id=body.user_id,
            job_id=body.job_id,
            video_url=stored.presigned_url,
        )
    except Exception as e:
        logger.error("[discord] send_video_preview failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    merged_script = _merge_script_json_with_media_names(
        existing_job.get("script_json") if existing_job else None,
        video_filename=normalized_video_filename,
    )
    video_storage_url = stored.s3_uri
    await job_service.update_job(
        body.job_id,
        confirm_message_id=message_id,
        video_url=video_storage_url,
        final_url=video_storage_url,
        script_json=merged_script,
    )
    logger.info("[discord] send_video_preview done job_id=%s", body.job_id)
    return {
        "job_id": body.job_id,
        "message_id": message_id,
        "video_filename": normalized_video_filename,
        "video_url": stored.presigned_url,
        "s3_uri": video_storage_url,
    }


@app.post("/internal/video-action")
async def video_action(_: AuthDep, body: VideoActionRequest) -> dict:
    """Discord 영상 승인/반려 버튼 클릭을 처리한다."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]

    if body.action == "approved":
        video_url = job.get("video_url", "")
        if isinstance(video_url, str) and video_url.startswith("s3://"):
            try:
                video_url = presign_s3_uri(video_url)
            except Exception as e:
                logger.error("[storage] presign video s3 uri failed job_id=%s: %s", body.job_id, e)
                raise HTTPException(status_code=500, detail=f"video presign failed: {e}")
        script_json = _as_script_json(job.get("script_json"))
        media_names = script_json.get("media_names") if isinstance(script_json.get("media_names"), dict) else {}
        video_filename = _normalize_video_filename(body.job_id, media_names.get("video_filename"), script_json)
        await job_service.transition_status(body.job_id, "PUBLISHING")

        try:
            await n8n_service.call_wf08_sns_upload(
                body.job_id,
                video_url,
                channel_id,
                video_filename=video_filename,
            )
        except Exception as e:
            logger.error("call_wf08_sns_upload failed job_id=%s: %s", body.job_id, e)

        logger.info("[discord] video_action=approved job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "approved"}

    elif body.action == "reject_select":
        try:
            await _discord_adapter.send_reject_step_buttons(channel_id, body.job_id)
        except Exception as e:
            logger.error("[discord] send_reject_step_buttons failed job_id=%s: %s", body.job_id, e)
            raise HTTPException(status_code=500, detail=str(e))

        logger.info("[discord] video_action=reject_select job_id=%s", body.job_id)
        return {"job_id": body.job_id, "action": "reject_select"}

    elif body.action == "reject_step":
        step = body.step or "draft"

        if step == "script":
            await job_service.transition_status(body.job_id, "WAITING_APPROVAL")
            confirm_message_id = job.get("confirm_message_id")
            script_json = job.get("script_json") or {}
            title = script_json.get("title", "대본")
            script_summary = script_json.get("script_summary") or script_json.get("script", "")[:100]
            try:
                new_msg_id = await _discord_adapter.send_confirm_message(
                    channel_id=channel_id,
                    user_id=user_id,
                    job_id=body.job_id,
                    title=title,
                    script_summary=script_summary,
                    preview_url=None,
                )
                await job_service.update_job(body.job_id, confirm_message_id=new_msg_id)
            except Exception as e:
                logger.error("[discord] re-send confirm failed job_id=%s: %s", body.job_id, e)

        elif step == "tts":
            await job_service.transition_status(body.job_id, "APPROVED")
            script_json = job.get("script_json") or {}
            script_text = script_json.get("script_text") or script_json.get("script", "")
            try:
                await n8n_service.call_wf11_tts_generate(
                    body.job_id,
                    script_text,
                    channel_id,
                    user_id,
                    auto_trigger_wf12=False,
                )
                await _discord_adapter.send_text_message(channel_id, "🔊 TTS를 재생성합니다...")
            except Exception as e:
                logger.error("call_wf11 (tts retry) failed job_id=%s: %s", body.job_id, e)

        elif step == "draft":
            await job_service.transition_status(body.job_id, "DRAFT")
            try:
                await _discord_adapter.send_text_message(channel_id, "🔄 처음부터 시작합니다. 새 콘셉트를 `/create`로 입력해주세요.")
            except Exception as e:
                logger.error("[discord] send_text_message failed job_id=%s: %s", body.job_id, e)

        logger.info("[discord] video_action=reject_step step=%s job_id=%s", step, body.job_id)
        return {"job_id": body.job_id, "action": "reject_step", "step": step}

    raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


@app.post("/internal/report-to-video")
async def report_to_video(_: AuthDep, body: ReportToVideoRequest) -> dict:
    """/report 결과의 '영상으로 제작' 버튼 클릭 처리 — WF-11(TTS) 직접 트리거."""
    job = await job_service.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    script_json = job.get("script_json") or {}
    script_text = script_json.get("script_text") or script_json.get("script", "")
    if not script_text:
        raise HTTPException(status_code=400, detail="No script found in job")

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]

    await job_service.transition_status(body.job_id, "APPROVED")

    try:
        await n8n_service.call_wf11_tts_generate(
            body.job_id,
            script_text,
            channel_id,
            user_id,
            auto_trigger_wf12=False,
        )
        await _discord_adapter.send_text_message(
            channel_id,
            "🔊 TTS 생성을 시작합니다. 완료본 확인 후 승인하면 WF-12(HeyGen)로 진행됩니다.",
        )
    except Exception as e:
        logger.error("call_wf11 (report_to_video) failed job_id=%s: %s", body.job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("[discord] report_to_video triggered job_id=%s", body.job_id)
    return {"job_id": body.job_id, "status": "triggered"}


@app.post("/internal/tts-generate")
async def tts_generate(_: AuthDep, body: ManualGenerateRequest) -> dict:
    """수동 /tts 명령 처리 — job_id(전체/접두) 또는 최근 작업으로 WF-11 실행."""
    logger.info(
        "[manual /tts] request job_id=%s user=%s channel=%s",
        (body.job_id or "").strip(),
        (body.messenger_user_id or "").strip(),
        (body.messenger_channel_id or "").strip(),
    )
    job = await _resolve_manual_job(body, require_script=True)
    resolved_job_id = job["id"]

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]
    script_json = job.get("script_json") or {}
    script_text = script_json.get("script_text") or script_json.get("script", "")
    if not script_text:
        raise HTTPException(status_code=400, detail="No script_text found in resolved job")

    await job_service.transition_status(resolved_job_id, "APPROVED")
    try:
        await n8n_service.call_wf11_tts_generate(
            resolved_job_id,
            script_text,
            channel_id,
            user_id,
            auto_trigger_wf12=False,
        )
    except Exception as e:
        logger.error("call_wf11 (manual /tts) failed job_id=%s: %s", resolved_job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("[manual /tts] triggered job_id=%s", resolved_job_id)
    return {"job_id": resolved_job_id, "status": "triggered", "workflow": "WF-11"}


@app.post("/internal/heygen-generate")
async def heygen_generate(_: AuthDep, body: ManualGenerateRequest) -> dict:
    """수동 /heygen 명령 처리 — job_id(전체/접두) 또는 최근 작업으로 WF-12 실행."""
    logger.info(
        "[manual /heygen] request job_id=%s user=%s channel=%s",
        (body.job_id or "").strip(),
        (body.messenger_user_id or "").strip(),
        (body.messenger_channel_id or "").strip(),
    )
    job = await _resolve_manual_job(body, require_audio=True)
    resolved_job_id = job["id"]

    channel_id = job["messenger_channel_id"]
    user_id = job["messenger_user_id"]
    audio_url = job.get("audio_url", "")
    if not audio_url:
        raise HTTPException(status_code=400, detail="No audio_url found in resolved job")

    audio_file_path = audio_url if isinstance(audio_url, str) and audio_url.startswith("/") else ""
    approved_audio_url = "" if audio_file_path else audio_url
    try:
        await n8n_service.call_wf12_heygen_generate(
            job_id=resolved_job_id,
            channel_id=channel_id,
            user_id=user_id,
            audio_file_path=audio_file_path,
            audio_url=approved_audio_url,
        )
    except Exception as e:
        logger.error("call_wf12 (manual /heygen) failed job_id=%s: %s", resolved_job_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("[manual /heygen] triggered job_id=%s", resolved_job_id)
    return {"job_id": resolved_job_id, "status": "triggered", "workflow": "WF-12"}


@app.post("/internal/jobs")
async def list_jobs(_: AuthDep, body: ListJobsRequest) -> dict:
    """Discord 수동 명령 보조용 최근 job 목록을 반환한다."""
    purpose = (body.purpose or "all").strip().lower()
    if purpose not in ("all", "tts", "heygen"):
        raise HTTPException(status_code=400, detail="purpose must be one of: all, tts, heygen")

    require_script = purpose == "tts"
    require_audio = purpose == "heygen"
    rows = await job_service.list_recent_jobs(
        body.messenger_user_id,
        body.messenger_channel_id,
        limit=body.limit,
        require_script=require_script,
        require_audio=require_audio,
    )

    jobs = [
        {
            "job_id": row["id"],
            "job_id_short": row["id"][:8],
            "status": row.get("status", ""),
            "created_at": _to_iso8601(row.get("created_at")),
            "updated_at": _to_iso8601(row.get("updated_at")),
            "has_script_text": bool((row.get("script_text") or "").strip()),
            "has_audio_url": bool((row.get("audio_url") or "").strip()),
        }
        for row in rows
    ]

    return {"purpose": purpose, "count": len(jobs), "jobs": jobs}


@app.get("/health")
async def health() -> dict:
    try:
        pool = await job_service.get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "connected"
    except Exception as e:
        logger.error("health check DB failed: %s", e)
        db_status = "error"

    return {
        "status": "ok",
        "db": db_status,
        "adapters": ["discord"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.gateway_port)
