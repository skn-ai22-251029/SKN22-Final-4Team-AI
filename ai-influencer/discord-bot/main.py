import asyncio
import logging
import uuid
from typing import Any, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands
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
    discord_bot_token: str
    discord_allowed_user_ids: str = ""
    discord_allowed_channel_ids: str = ""
    discord_guild_id: str = ""
    gateway_url: str = "http://messenger-gateway:8080"
    gateway_internal_secret: str

    class Config:
        env_file = ".env"
        case_sensitive = False


config = Settings()
ALLOWED_CHANNEL_IDS: set[str] = {
    cid.strip() for cid in config.discord_allowed_channel_ids.split(",") if cid.strip()
}
SYNC_GUILD_IDS: list[int] = [
    int(gid.strip())
    for gid in config.discord_guild_id.split(",")
    if gid.strip().isdigit()
]


# ─────────────────────────────────────────
# Gateway 클라이언트 헬퍼
# ─────────────────────────────────────────

_gateway_client: Optional[httpx.AsyncClient] = None


def get_gateway_client() -> httpx.AsyncClient:
    # Discord bot은 모든 실제 비즈니스 처리를 gateway에 위임하므로
    # base_url/secret 이 고정된 단일 HTTP 클라이언트를 재사용한다.
    global _gateway_client
    if _gateway_client is None:
        _gateway_client = httpx.AsyncClient(
            base_url=config.gateway_url,
            headers={"X-Internal-Secret": config.gateway_internal_secret},
            timeout=30.0,
        )
    return _gateway_client


def _error_detail_from_response(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = data.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail
    except Exception:
        pass
    body = resp.text.strip()
    return body or f"HTTP {resp.status_code}"


def _gateway_transport_error_detail(path: str, exc: Exception) -> str:
    normalized_path = (path or "").strip() or "/"
    if normalized_path == "/internal/seedlab-start":
        if isinstance(exc, httpx.ReadTimeout):
            return "Seed Lab start timed out while waiting for gateway response. 게이트웨이 응답 대기 중 시간이 초과되었습니다."
        if isinstance(exc, httpx.ConnectError):
            return "Seed Lab start could not reach gateway. 게이트웨이에 연결하지 못했습니다."
        if isinstance(exc, httpx.TransportError):
            return (
                f"Seed Lab start gateway transport error: {type(exc).__name__}. "
                "게이트웨이 통신 중 오류가 발생했습니다."
            )
    if isinstance(exc, httpx.ReadTimeout):
        return f"Gateway request timed out: {normalized_path}. 게이트웨이 응답 대기 중 시간이 초과되었습니다."
    if isinstance(exc, httpx.ConnectError):
        return f"Gateway connection failed: {normalized_path}. 게이트웨이에 연결하지 못했습니다."
    if isinstance(exc, httpx.TransportError):
        return f"Gateway transport error: {type(exc).__name__} ({normalized_path}). 게이트웨이 통신 오류가 발생했습니다."
    return ""


async def gateway_call(path: str, payload: dict) -> dict[str, Any]:
    # slash command / 버튼 이벤트는 모두 이 헬퍼를 통해 gateway 내부 API로 전달된다.
    try:
        resp = await get_gateway_client().post(path, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(_error_detail_from_response(resp))
        if not resp.content:
            return {}
        try:
            parsed = resp.json()
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}
    except (httpx.ReadTimeout, httpx.ConnectError, httpx.TransportError) as e:
        detail = _gateway_transport_error_detail(path, e) or f"Gateway request failed: {type(e).__name__}"
        logger.error("[discord] gateway_call %s failed: %s", path, detail)
        raise RuntimeError(detail) from e
    except Exception as e:
        logger.error("[discord] gateway_call %s failed: %s", path, e)
        raise


def _clip_text(text: str, max_len: int = 1500) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


async def _safe_reply(interaction: discord.Interaction, text: str, *, ephemeral: bool = True) -> None:
    message = _clip_text((text or "").strip() or "⚠️ 요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)
        return
    except Exception as e:
        logger.error("[discord] safe reply failed: %s", e)

    # 마지막 폴백: 채널 메시지 (ephemeral 보장은 불가)
    try:
        if interaction.channel:
            await interaction.channel.send("⚠️ 명령 응답 전송에 실패했습니다. 잠시 후 다시 시도해주세요.")
    except Exception as e:
        logger.error("[discord] fallback channel reply failed: %s", e)


async def _safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(ephemeral=ephemeral)
    except Exception as e:
        logger.error("[discord] defer failed: %s", e)


def _build_button_view(*buttons: tuple[str, str, discord.ButtonStyle]) -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    for label, custom_id, style in buttons:
        view.add_item(discord.ui.Button(label=label, custom_id=custom_id, style=style))
    return view


def _is_channel_allowed(channel_id: Optional[int]) -> bool:
    if not ALLOWED_CHANNEL_IDS:
        return True
    return str(channel_id or "") in ALLOWED_CHANNEL_IDS


async def _reject_if_channel_disallowed(interaction: discord.Interaction) -> bool:
    if _is_channel_allowed(interaction.channel_id):
        return False
    await _safe_reply(interaction, "이 채널에서는 사용할 수 없습니다.", ephemeral=True)
    return True


# ─────────────────────────────────────────
# 인메모리 수정 대기 상태 (pending_store)
# ─────────────────────────────────────────

revision_pending: dict[str, str] = {}  # user_id -> job_id
publish_title_pending: dict[str, dict[str, Any]] = {}  # token -> {user_id, job_id, targets, label, publish_title}


class _YoutubeTitleModal(discord.ui.Modal, title="유튜브 업로드 제목"):
    publish_title_input = discord.ui.TextInput(
        label="유튜브 제목 (비우면 자동 제목 사용)",
        style=discord.TextStyle.short,
        required=False,
        max_length=95,
    )

    def __init__(self, token: str):
        super().__init__(timeout=300)
        self.token = token

    async def on_submit(self, interaction: discord.Interaction) -> None:
        intent = publish_title_pending.get(self.token)
        if not intent:
            await _safe_reply(interaction, "⏱️ 제목 입력 세션이 만료되었습니다. 다시 업로드 버튼을 눌러주세요.", ephemeral=True)
            return

        if str(intent.get("user_id", "")) != str(interaction.user.id):
            await _safe_reply(interaction, "이 제목 입력 세션은 요청한 사용자만 사용할 수 있습니다.", ephemeral=True)
            return

        publish_title = str(self.publish_title_input.value or "").strip()
        intent["publish_title"] = publish_title

        label = str(intent.get("label") or "유튜브")
        view = _build_button_view(
            (f"✅ {label} 최종 승인", f"video_publish_confirm_title:{self.token}", discord.ButtonStyle.danger),
            ("취소", f"video_publish_cancel_title:{self.token}", discord.ButtonStyle.secondary),
        )
        title_preview = publish_title if publish_title else "(자동 제목 사용)"
        await interaction.response.send_message(
            f"⚠️ {label} 업로드를 시작합니다. 최종 승인하면 WF-08 SNS 업로드를 실행합니다.\n"
            f"제목: `{_clip_text(title_preview, 95)}`",
            ephemeral=True,
            view=view,
        )


class _TtsAvatarIdModal(discord.ui.Modal, title="HeyGen 아바타 ID 입력"):
    avatar_id_input = discord.ui.TextInput(
        label="avatar_id",
        placeholder="예: b903a1fd1ec846e0ba2e89620bc0aaae",
        style=discord.TextStyle.short,
        required=True,
        max_length=160,
    )

    def __init__(self, job_id: str):
        super().__init__(timeout=300)
        self.job_id = job_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        avatar_id = str(self.avatar_id_input.value or "").strip()
        if not avatar_id:
            await _safe_reply(interaction, "avatar_id를 입력해주세요.", ephemeral=True)
            return
        await _safe_defer(interaction, ephemeral=True)
        try:
            result = await gateway_call(
                "/internal/tts-action",
                {
                    "job_id": self.job_id,
                    "action": "select_avatar_custom",
                    "avatar_id": avatar_id,
                },
            )
            avatar_label = str(result.get("avatar_label") or f"직접입력:{avatar_id[:8]}")
            await interaction.followup.send(
                f"👤 아바타ID `{avatar_label}`을 선택했습니다. 이제 일반 승인 또는 고화질 승인을 진행하세요.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"오류가 발생했습니다: {e}", ephemeral=True)


# ─────────────────────────────────────────
# Discord Bot
# ─────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    synced_global_count = 0
    try:
        target_guild_ids = SYNC_GUILD_IDS or [guild.id for guild in bot.guilds]
        if target_guild_ids:
            for guild_id in target_guild_ids:
                guild = discord.Object(id=guild_id)
                bot.tree.copy_global_to(guild=guild)
                synced_guild = await bot.tree.sync(guild=guild)
                logger.info("[discord] guild sync done guild_id=%s commands=%d", guild_id, len(synced_guild))

        synced_global = await bot.tree.sync()
        synced_global_count = len(synced_global)
    except Exception as e:
        logger.error("[discord] slash command sync failed: %s", e)

    logger.info(
        "[discord] bot online: %s / slash commands synced(global=%d)",
        bot.user,
        synced_global_count,
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    logger.exception("[discord] app command error: %s", error)
    await _safe_reply(interaction, f"❌ 명령 처리 실패: {_clip_text(str(error), 500)}", ephemeral=True)


@bot.event
async def on_message(message: discord.Message) -> None:
    # 봇 자신의 메시지 무시
    if message.author.bot:
        return

    # 허용 채널 확인
    if not _is_channel_allowed(message.channel.id):
        return

    user_id = str(message.author.id)

    # 수정 지시 텍스트 대기 중인 경우에만 처리
    if user_id in revision_pending:
        # 버튼으로 "수정 지시"를 누른 직후 사용자가 보내는 다음 메시지를
        # revision_note로 간주해 confirm-action으로 전달한다.
        job_id = revision_pending.pop(user_id)
        try:
            await gateway_call(
                "/internal/confirm-action",
                {
                    "job_id": job_id,
                    "action": "revision_requested",
                    "revision_note": message.content,
                },
            )
        except Exception:
            pass


@bot.tree.command(name="create", description="AI 콘텐츠 생성을 요청합니다")
async def create_command(interaction: discord.Interaction, concept: str) -> None:
    user_id = str(interaction.user.id)

    if await _reject_if_channel_disallowed(interaction):
        return

    await interaction.response.defer()

    # Discord에서는 job_id만 만들고, 실제 job 생성과 WF-01 시작은 gateway가 맡는다.
    job_id = str(uuid.uuid4())
    image_url = interaction.message.attachments[0].url if interaction.message and interaction.message.attachments else None

    try:
        await gateway_call(
            "/internal/message",
            {
                "job_id": job_id,
                "messenger_source": "discord",
                "messenger_user_id": user_id,
                "messenger_channel_id": str(interaction.channel_id),
                "concept_text": concept,
                "ref_image_url": image_url,
                "character_id": "default-character",
            },
        )
        await interaction.followup.send(
            f"✅ 요청이 접수되었습니다!\nJob ID: {job_id[:8]}...\n콘셉트: {concept[:50]}...\n\n잠시 후 처리 결과를 알려드릴게요. ⏳"
        )
    except Exception:
        await interaction.followup.send("요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


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


@bot.tree.command(name="report", description="NotebookLM 보고서를 생성합니다")
async def report_command(
    interaction: discord.Interaction,
    prompt: Optional[str] = None,
) -> None:
    user_id = str(interaction.user.id)

    if await _reject_if_channel_disallowed(interaction):
        return

    await interaction.response.defer()

    # /report는 사용자가 프롬프트를 비워도 동작하도록 기본 템플릿을 앞에 붙여 보낸다.
    job_id = str(uuid.uuid4())
    cleaned_prompt = (prompt or "").strip()
    merged_prompt = _REPORT_SYSTEM_PROMPT + cleaned_prompt
    try:
        await gateway_call(
            "/internal/report-message",
            {
                "job_id": job_id,
                "messenger_source": "discord",
                "messenger_user_id": user_id,
                "messenger_channel_id": str(interaction.channel_id),
                "prompt": merged_prompt,
                "notebook_id": "",
                "channel_id": "",
                "character_id": "default-character",
            },
        )
        if cleaned_prompt:
            await interaction.followup.send(
                f"📊 요청 접수! 채널을 선택하면 보고서를 가져옵니다. ⏳\nJob ID: `{job_id[:8]}`"
            )
        else:
            await interaction.followup.send(
                f"📊 요청 접수! (프롬프트 공백 허용: 기본 템플릿으로 진행)\n"
                f"채널을 선택하면 보고서를 가져옵니다. ⏳\nJob ID: `{job_id[:8]}`"
            )
    except Exception:
        await interaction.followup.send("보고서 요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


@bot.tree.command(name="tts", description="기존 job_id 또는 직접 프롬프트로 TTS 후보 3개 생성을 시작합니다")
@app_commands.describe(
    job_id="기존 job_id 또는 앞 8자리. 비우면 최근 job을 자동 선택합니다.",
    prompt="직접 TTS로 만들 대본. 입력 시 새 job을 생성합니다.",
)
async def tts_command(
    interaction: discord.Interaction,
    job_id: str = "",
    prompt: str = "",
) -> None:
    user_id = str(interaction.user.id)
    normalized_job_id = (job_id or "").strip()
    normalized_prompt = (prompt or "").strip()
    logger.info(
        "[/tts] invoked user=%s channel=%s job_input=%s prompt_len=%d",
        user_id,
        interaction.channel_id,
        normalized_job_id,
        len(normalized_prompt),
    )

    try:
        if await _reject_if_channel_disallowed(interaction):
            return

        if normalized_job_id and normalized_prompt:
            await _safe_reply(
                interaction,
                "`job_id`와 `prompt`는 동시에 사용할 수 없습니다. 기존 job으로 돌릴지, 새 대본으로 시작할지 하나만 선택하세요.",
                ephemeral=True,
            )
            return

        await _safe_defer(interaction, ephemeral=True)
        # job_id가 비어 있으면 gateway가 현재 사용자/채널의 최근 적합 job을 선택한다.
        result = await gateway_call(
            "/internal/tts-generate",
            {
                "job_id": normalized_job_id,
                "messenger_user_id": user_id,
                "messenger_channel_id": str(interaction.channel_id),
                "prompt": normalized_prompt,
            },
        )
        resolved_job_id = (result.get("job_id") or normalized_job_id).strip()
        if not resolved_job_id:
            raise RuntimeError("gateway returned empty job_id")

        if normalized_prompt:
            message = f"🔊 새 대본으로 TTS 후보 3개 생성 요청 완료: `{resolved_job_id[:8]}`"
        else:
            picked_latest = not normalized_job_id
            message = (
                "🔊 TTS 후보 3개 생성 요청 완료 "
                f"(자동 선택: 최근 job): `{resolved_job_id[:8]}`"
                if picked_latest
                else f"🔊 TTS 후보 3개 생성 요청 완료: `{resolved_job_id[:8]}`"
            )
        await _safe_reply(interaction, message, ephemeral=True)
        logger.info("[/tts] success user=%s resolved_job_id=%s", user_id, resolved_job_id)
    except Exception as e:
        logger.exception(
            "[/tts] failed user=%s channel=%s job_input=%s prompt_len=%d",
            user_id,
            interaction.channel_id,
            normalized_job_id,
            len(normalized_prompt),
        )
        await _safe_reply(interaction, f"❌ /tts 실패: {_clip_text(str(e), 500)}", ephemeral=True)


@bot.tree.command(name="seedlab", description="AWS Seed Lab run을 시작하고 웹 링크를 반환합니다")
@app_commands.describe(
    seeds="쉼표로 구분한 seed 목록. 비우면 랜덤으로 채웁니다.",
    dup="켜면 seed당 3개씩(10 x 3), 끄면 30 x 1입니다.",
)
async def seedlab_command(
    interaction: discord.Interaction,
    seeds: str = "",
    dup: bool = False,
) -> None:
    user_id = str(interaction.user.id)
    logger.info("[/seedlab] invoked user=%s channel=%s dup=%s seeds=%s", user_id, interaction.channel_id, dup, (seeds or "").strip())

    try:
        if await _reject_if_channel_disallowed(interaction):
            return

        await _safe_defer(interaction, ephemeral=True)
        result = await gateway_call(
            "/internal/seedlab-start",
            {
                "messenger_user_id": user_id,
                "messenger_channel_id": str(interaction.channel_id),
                "seeds": (seeds or "").strip(),
                "dup": bool(dup),
            },
        )
        await _safe_reply(
            interaction,
            _clip_text(
                "🧪 Seed Lab run 생성 완료\n"
                f"Run ID: `{str(result.get('run_id') or '')}`\n"
                f"mode: {int(result.get('samples') or (10 if dup else 30))} x {int(result.get('takes_per_seed') or (3 if dup else 1))}\n"
                f"link: {str(result.get('seedlab_url') or '')}\n"
                "진행 상황은 채널 메시지에서 계속 갱신됩니다."
            ),
            ephemeral=True,
        )
    except Exception as e:
        logger.exception("[/seedlab] failed user=%s channel=%s", user_id, interaction.channel_id)
        await _safe_reply(interaction, f"❌ /seedlab 실패: {_clip_text(str(e), 500)}", ephemeral=True)


@bot.tree.command(name="cost", description="Cost Viewer signed 링크를 반환합니다")
async def cost_command(interaction: discord.Interaction) -> None:
    user_id = str(interaction.user.id)
    logger.info("[/cost] invoked user=%s channel=%s", user_id, interaction.channel_id)

    try:
        if await _reject_if_channel_disallowed(interaction):
            return

        await _safe_defer(interaction, ephemeral=True)
        result = await gateway_call(
            "/internal/cost-viewer-link",
            {
                "messenger_user_id": user_id,
                "messenger_channel_id": str(interaction.channel_id),
            },
        )
        await _safe_reply(
            interaction,
            _clip_text(
                "💰 Cost Viewer 링크 생성 완료\n"
                f"link: {str(result.get('cost_viewer_url') or '')}\n"
                f"expires_at: {str(result.get('expires_at') or '')}"
            ),
            ephemeral=True,
        )
    except Exception as e:
        logger.exception("[/cost] failed user=%s channel=%s", user_id, interaction.channel_id)
        await _safe_reply(interaction, f"❌ /cost 실패: {_clip_text(str(e), 500)}", ephemeral=True)


@bot.tree.command(name="heygen", description="기존 job_id로 WF-12(HeyGen) 생성을 시작합니다")
async def heygen_command(interaction: discord.Interaction, job_id: str = "") -> None:
    user_id = str(interaction.user.id)
    logger.info("[/heygen] invoked user=%s channel=%s job_input=%s", user_id, interaction.channel_id, (job_id or "").strip())

    try:
        if await _reject_if_channel_disallowed(interaction):
            return

        await _safe_reply(
            interaction,
            "🎬 `/heygen` 명령은 비활성화되었습니다. TTS 완료 메시지의 `일반 승인` 또는 `고화질 승인` 버튼에서만 WF-12를 실행할 수 있습니다.",
            ephemeral=True,
        )
        logger.info("[/heygen] disabled user=%s", user_id)
    except Exception as e:
        logger.exception(
            "[/heygen] failed user=%s channel=%s job_input=%s",
            user_id,
            interaction.channel_id,
            (job_id or "").strip(),
        )
        await _safe_reply(interaction, f"❌ /heygen 실패: {_clip_text(str(e), 500)}", ephemeral=True)


@bot.tree.command(name="jobs", description="최근 job 목록을 조회합니다")
async def jobs_command(interaction: discord.Interaction, purpose: str = "all") -> None:
    user_id = str(interaction.user.id)

    if await _reject_if_channel_disallowed(interaction):
        return

    normalized_purpose = (purpose or "all").strip().lower()
    if normalized_purpose not in ("all", "tts", "heygen"):
        await interaction.response.send_message(
            "purpose는 all / tts / heygen 중 하나여야 합니다.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        result = await gateway_call(
            "/internal/jobs",
            {
                "messenger_user_id": user_id,
                "messenger_channel_id": str(interaction.channel_id),
                "purpose": normalized_purpose,
                "limit": 5,
            },
        )
        jobs = result.get("jobs", [])
        if not jobs:
            await interaction.followup.send("최근 job이 없습니다.", ephemeral=True)
            return

        lines = []
        for item in jobs:
            jid = item.get("job_id_short", "")
            status = item.get("status", "")
            has_script = "Y" if item.get("has_script_text") else "N"
            has_audio = "Y" if item.get("has_audio_url") else "N"
            lines.append(f"`{jid}` status={status} script={has_script} audio={has_audio}")

        guide = "사용: `/tts`에 위 8자리 job_id를 넣거나, job_id 없이 실행. 영상은 TTS 완료 메시지의 승인 버튼에서 진행"
        await interaction.followup.send(
            f"최근 job 목록(purpose={normalized_purpose}):\n" + "\n".join(lines) + f"\n\n{guide}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ /jobs 실패: {e}", ephemeral=True)


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    # 버튼 클릭(컴포넌트 인터랙션)만 처리
    if interaction.type != discord.InteractionType.component:
        return

    custom_id: str = interaction.data.get("custom_id", "")
    if ":" not in custom_id:
        await interaction.response.send_message("잘못된 요청입니다.", ephemeral=True)
        return

    # custom_id 포맷만 보고 어떤 gateway endpoint를 칠지 결정한다.
    parts = custom_id.split(":")
    action = parts[0]
    # video_reject_step: video_reject_step:{job_id}:{step}
    # video_publish_youtube: video_publish_youtube:{job_id}
    # video_publish_instagram: video_publish_instagram:{job_id}
    # video_publish_both: video_publish_both:{job_id}
    # video_publish_confirm_youtube: video_publish_confirm_youtube:{job_id}
    # video_publish_confirm_instagram: video_publish_confirm_instagram:{job_id}
    # video_publish_confirm_both: video_publish_confirm_both:{job_id}
    # video_publish_cancel: video_publish_cancel:{job_id}
    # tts_approve_standard:         tts_approve_standard:{job_id}
    # tts_approve_standard_confirm: tts_approve_standard_confirm:{job_id}
    # tts_approve_standard_cancel:  tts_approve_standard_cancel:{job_id}
    # tts_approve_hd:               tts_approve_hd:{job_id}
    # tts_approve_hd_confirm:       tts_approve_hd_confirm:{job_id}
    # tts_approve_hd_cancel:        tts_approve_hd_cancel:{job_id}
    # tts_avatar_pick:              tts_avatar_pick:{job_id}:{avatar_index}
    # tts_avatar_custom:            tts_avatar_custom:{job_id}
    # tts_select:                   tts_select:{job_id}:{batch_id}:{variant_index}
    # tts_regenerate:               tts_regenerate:{job_id}:{batch_id}
    # tts_reject:        tts_reject:{job_id}
    # report_to_tts:     report_to_tts:{job_id}
    # report_to_video:         report_to_video:{job_id}
    # report_to_video_confirm: report_to_video_confirm:{job_id}
    # report_to_video_cancel:  report_to_video_cancel:{job_id}
    # select_report:     select_report:{job_id}:{channel_id}:{index}
    # new_report:        new_report:{job_id}:{channel_id}
    # select_channel:    select_channel:{job_id}:{channel_id}
    step = None
    report_index = None
    channel_id_value = None
    batch_id = ""
    variant_index = None
    avatar_index = None
    publish_token = ""
    # 버튼 종류마다 인코딩된 파라미터 수가 달라서 여기서 먼저 분해한다.
    if action == "video_reject_step" and len(parts) >= 3:
        job_id = parts[1]
        step = parts[2]
    elif action == "select_report" and len(parts) >= 4:
        job_id = parts[1]
        channel_id_value = parts[2]
        report_index = int(parts[3])
    elif action == "new_report" and len(parts) >= 3:
        job_id = parts[1]
        channel_id_value = parts[2]
    elif action == "select_channel" and len(parts) >= 3:
        job_id = parts[1]
        channel_id_value = ":".join(parts[2:])
    elif action == "tts_select" and len(parts) >= 4:
        job_id = parts[1]
        batch_id = parts[2]
        variant_index = int(parts[3])
    elif action == "tts_regenerate" and len(parts) >= 3:
        job_id = parts[1]
        batch_id = parts[2]
    elif action == "tts_avatar_pick" and len(parts) >= 3:
        job_id = parts[1]
        avatar_index = int(parts[2])
    elif action == "tts_avatar_custom" and len(parts) >= 2:
        job_id = parts[1]
    elif action in {"video_publish_confirm_title", "video_publish_cancel_title"} and len(parts) >= 2:
        publish_token = parts[1]
        pending = publish_title_pending.get(publish_token) or {}
        job_id = str(pending.get("job_id") or "")
    elif action in {
        "tts_approve_standard",
        "tts_approve_standard_confirm",
        "tts_approve_standard_cancel",
        "tts_approve_hd",
        "tts_approve_hd_confirm",
        "tts_approve_hd_cancel",
        "tts_reject",
        "video_publish_youtube",
        "video_publish_instagram",
        "video_publish_both",
        "video_publish_confirm_youtube",
        "video_publish_confirm_instagram",
        "video_publish_confirm_both",
        "video_publish_cancel",
        "report_to_tts",
        "report_to_video",
        "report_to_video_confirm",
        "report_to_video_cancel",
        "approve",
        "revise",
        "video_approve",
        "video_reject",
    } and len(parts) >= 2:
        job_id = parts[-1]
    else:
        job_id = ":".join(parts[1:])
    user_id = str(interaction.user.id)

    if await _reject_if_channel_disallowed(interaction):
        return
        return

    if action in {"video_publish_youtube", "video_publish_both"}:
        try:
            if action == "video_publish_youtube":
                targets = ["youtube"]
                label = "유튜브"
            else:
                targets = ["youtube", "instagram"]
                label = "유튜브 + 인스타그램"
            token = uuid.uuid4().hex[:12]
            publish_title_pending[token] = {
                "user_id": user_id,
                "job_id": job_id,
                "targets": targets,
                "label": label,
                "publish_title": "",
            }
            await interaction.response.send_modal(_YoutubeTitleModal(token))
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")
        return

    # 구형 확인 버튼(legacy custom_id)도 제목 모달 경로로 강제한다.
    if action in {"video_publish_confirm_youtube", "video_publish_confirm_both"}:
        try:
            if action == "video_publish_confirm_youtube":
                targets = ["youtube"]
                label = "유튜브"
            else:
                targets = ["youtube", "instagram"]
                label = "유튜브 + 인스타그램"
            token = uuid.uuid4().hex[:12]
            publish_title_pending[token] = {
                "user_id": user_id,
                "job_id": job_id,
                "targets": targets,
                "label": label,
                "publish_title": "",
            }
            await interaction.response.send_modal(_YoutubeTitleModal(token))
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")
        return

    if action == "tts_avatar_custom":
        try:
            await interaction.response.send_modal(_TtsAvatarIdModal(job_id))
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")
        return

    # Discord 컴포넌트는 3초 안에 응답해야 하므로 먼저 defer하고 실제 처리는 뒤에서 한다.
    await _safe_defer(interaction, ephemeral=True)

    if action == "approve":
        # 스크립트 승인 -> gateway confirm-action -> WF-05 -> WF-11 경로로 이어진다.
        try:
            await gateway_call(
                "/internal/confirm-action",
                {"job_id": job_id, "action": "approved"},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "revise":
        # 수정 지시는 별도 모달 대신 "다음 일반 메시지 1개"를 revision_note로 받는다.
        revision_pending[user_id] = job_id
        await interaction.channel.send("✏️ 어떤 점을 수정할까요? 구체적으로 입력해주세요.")

    elif action == "video_approve":
        try:
            view = _build_button_view(
                ("📺 유튜브 업로드", f"video_publish_youtube:{job_id}", discord.ButtonStyle.primary),
                ("📸 인스타 업로드", f"video_publish_instagram:{job_id}", discord.ButtonStyle.primary),
                ("📺📸 둘 다 업로드", f"video_publish_both:{job_id}", discord.ButtonStyle.success),
                ("취소", f"video_publish_cancel:{job_id}", discord.ButtonStyle.secondary),
            )
            await interaction.followup.send(
                "업로드할 플랫폼을 선택하세요. 선택 후 한 번 더 최종 확인합니다.",
                ephemeral=True,
                view=view,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "video_reject":
        # 즉시 반려하지 않고, 어느 단계(script/tts/draft)로 되돌릴지 한 번 더 묻는다.
        try:
            await gateway_call(
                "/internal/video-action",
                {"job_id": job_id, "action": "reject_select"},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "video_reject_step":
        # reject_step은 이미 선택된 되돌림 단계를 gateway에 그대로 전달한다.
        try:
            await gateway_call(
                "/internal/video-action",
                {"job_id": job_id, "action": "reject_step", "step": step},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action in {"video_publish_instagram"}:
        try:
            label = "인스타그램"
            confirm_action = "video_publish_confirm_instagram"

            view = _build_button_view(
                (f"✅ {label} 최종 승인", f"{confirm_action}:{job_id}", discord.ButtonStyle.danger),
                ("취소", f"video_publish_cancel:{job_id}", discord.ButtonStyle.secondary),
            )
            await interaction.followup.send(
                f"⚠️ {label} 업로드를 시작합니다. 최종 승인하면 WF-08 SNS 업로드를 실행합니다.",
                ephemeral=True,
                view=view,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action in {"video_publish_confirm_title", "video_publish_cancel_title"}:
        intent = publish_title_pending.get(publish_token)
        if not intent:
            await interaction.followup.send("⏱️ 제목 입력 세션이 만료되었습니다. 다시 업로드 버튼을 눌러주세요.", ephemeral=True)
            return
        if str(intent.get("user_id", "")) != user_id:
            await interaction.followup.send("이 제목 입력 세션은 요청한 사용자만 사용할 수 있습니다.", ephemeral=True)
            return

        if action == "video_publish_cancel_title":
            publish_title_pending.pop(publish_token, None)
            await interaction.followup.send("SNS 업로드 요청을 취소했습니다.", ephemeral=True)
            return

        try:
            targets = list(intent.get("targets") or [])
            label = str(intent.get("label") or "유튜브")
            publish_title = str(intent.get("publish_title") or "").strip()
            result = await gateway_call(
                "/internal/video-action",
                {
                    "job_id": str(intent.get("job_id") or job_id),
                    "action": "approved",
                    "targets": targets,
                    "publish_title": publish_title,
                },
            )
            publish_title_pending.pop(publish_token, None)
            result_action = str(result.get("action") or "").strip()
            if result_action == "already_published":
                await interaction.followup.send(
                    f"ℹ️ {label} 업로드는 이미 완료되어 있어 중복 실행하지 않았습니다.",
                    ephemeral=True,
                )
            elif result_action == "already_publishing":
                await interaction.followup.send(
                    f"⏳ {label} 업로드가 이미 진행 중입니다. 완료 메시지를 기다려주세요.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"✅ {label} 업로드를 시작합니다. WF-08 실행 중...",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action in {"video_publish_confirm_instagram"}:
        try:
            if action == "video_publish_confirm_instagram":
                targets = ["instagram"]
                label = "인스타그램"
            result = await gateway_call(
                "/internal/video-action",
                {"job_id": job_id, "action": "approved", "targets": targets},
            )
            result_action = str(result.get("action") or "").strip()
            if result_action == "already_published":
                await interaction.followup.send(
                    f"ℹ️ {label} 업로드는 이미 완료되어 있어 중복 실행하지 않았습니다.",
                    ephemeral=True,
                )
            elif result_action == "already_publishing":
                await interaction.followup.send(
                    f"⏳ {label} 업로드가 이미 진행 중입니다. 완료 메시지를 기다려주세요.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"✅ {label} 업로드를 시작합니다. WF-08 실행 중...",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "video_publish_cancel":
        await interaction.followup.send("SNS 업로드 요청을 취소했습니다.", ephemeral=True)

    elif action == "tts_approve_standard":
        try:
            view = _build_button_view(
                ("✅ 일반 최종 승인", f"tts_approve_standard_confirm:{job_id}", discord.ButtonStyle.primary),
                ("취소", f"tts_approve_standard_cancel:{job_id}", discord.ButtonStyle.secondary),
            )
            await interaction.followup.send(
                "⚠️ 일반 모드로 영상을 생성합니다. 최종 승인하면 WF-12(HeyGen) 일반 모드를 실행합니다.",
                ephemeral=True,
                view=view,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "tts_approve_standard_confirm":
        try:
            await gateway_call(
                "/internal/tts-action",
                {"job_id": job_id, "action": "approve_standard", "use_avatar_iv_model": False},
            )
            await interaction.followup.send("✅ 일반 승인됨. WF-12(HeyGen) 일반 모드 실행 중...", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "tts_approve_standard_cancel":
        await interaction.followup.send("일반 승인 요청을 취소했습니다.", ephemeral=True)

    elif action == "tts_approve_hd":
        try:
            view = _build_button_view(
                ("💎 고화질 최종 승인", f"tts_approve_hd_confirm:{job_id}", discord.ButtonStyle.danger),
                ("취소", f"tts_approve_hd_cancel:{job_id}", discord.ButtonStyle.secondary),
            )
            await interaction.followup.send(
                "⚠️ 고화질 Avatar IV 모드는 추가 비용이 발생할 수 있습니다. 최종 승인하면 WF-12를 고화질 모드로 실행합니다.",
                ephemeral=True,
                view=view,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "tts_approve_hd_confirm":
        try:
            await gateway_call(
                "/internal/tts-action",
                {"job_id": job_id, "action": "approve_hd", "use_avatar_iv_model": True},
            )
            await interaction.followup.send("💎 고화질 승인됨. WF-12(HeyGen) Avatar IV 모드 실행 중...", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "tts_approve_hd_cancel":
        await interaction.followup.send("고화질 승인 요청을 취소했습니다.", ephemeral=True)

    elif action == "tts_select":
        try:
            await gateway_call(
                "/internal/tts-action",
                {
                    "job_id": job_id,
                    "action": "select_variant",
                    "batch_id": batch_id,
                    "variant_index": variant_index,
                },
            )
            await interaction.followup.send(
                f"✅ TTS 후보 {int(variant_index) + 1}번을 선택했습니다. 다음 단계 버튼을 확인하세요.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "tts_regenerate":
        try:
            await gateway_call(
                "/internal/tts-action",
                {
                    "job_id": job_id,
                    "action": "regenerate_batch",
                    "batch_id": batch_id,
                },
            )
            await interaction.followup.send("🔁 TTS 후보 3개를 다시 생성합니다.", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "tts_avatar_pick":
        try:
            if avatar_index is None:
                raise RuntimeError("avatar_index is required")
            result = await gateway_call(
                "/internal/tts-action",
                {
                    "job_id": job_id,
                    "action": "select_avatar",
                    "avatar_index": avatar_index,
                },
            )
            avatar_label = str(result.get("avatar_label") or f"#{avatar_index}")
            await interaction.followup.send(
                f"👤 아바타를 `{avatar_label}`(으)로 선택했습니다. 이제 일반 승인 또는 고화질 승인을 진행하세요.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "tts_reject":
        # TTS 반려는 job을 대본 승인 상태로 되돌려 수동 재생성을 가능하게 한다.
        try:
            await gateway_call(
                "/internal/tts-action",
                {"job_id": job_id, "action": "reject"},
            )
            await interaction.followup.send("❌ TTS 반려 처리되었습니다.", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "report_to_video":
        try:
            view = _build_button_view(
                ("계속", f"report_to_video_confirm:{job_id}", discord.ButtonStyle.primary),
                ("취소", f"report_to_video_cancel:{job_id}", discord.ButtonStyle.secondary),
            )
            await interaction.followup.send(
                "⚠️ 영상 제작 모드로 전환하면 먼저 TTS를 생성하고, 완료 후 일반 승인 또는 고화질 승인을 다시 선택하게 됩니다.",
                ephemeral=True,
                view=view,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "report_to_video_confirm":
        try:
            await gateway_call(
                "/internal/report-to-video",
                {"job_id": job_id},
            )
            await interaction.followup.send(
                "🎬 영상 제작 준비를 시작합니다. TTS 후보 3개 중 하나를 선택한 뒤 일반 승인 또는 고화질 승인을 선택하고, 최종 확인 후 영상을 생성하세요.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "report_to_video_cancel":
        await interaction.followup.send("영상 제작 전환을 취소했습니다.", ephemeral=True)

    elif action == "report_to_tts":
        # 보고서 결과에서 TTS만 제작하는 분기.
        try:
            await gateway_call(
                "/internal/report-to-tts",
                {"job_id": job_id},
            )
            await interaction.followup.send("🔊 TTS 제작을 시작합니다. 완료 후 승인/반려를 선택하세요.", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "select_report":
        # 기존 보고서 선택 -> gateway가 get-report 또는 새 생성 fallback을 결정한다.
        try:
            await gateway_call(
                "/internal/report-select",
                {
                    "job_id": job_id,
                    "action": "select",
                    "report_index": report_index,
                    "channel_id": channel_id_value or "",
                },
            )
            await interaction.followup.send("📄 선택한 보고서를 가져오는 중입니다...", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "new_report":
        # 새 보고서 생성 -> gateway가 WF-06을 새로 트리거한다.
        try:
            await gateway_call(
                "/internal/report-select",
                {"job_id": job_id, "action": "new", "channel_id": channel_id_value or ""},
            )
            await interaction.followup.send("🆕 새 보고서 생성을 시작합니다...", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "select_channel":
        # 채널 선택 -> gateway background task가 list-reports를 조회하고 버튼을 다시 내려준다.
        try:
            await gateway_call(
                "/internal/channel-select",
                {"job_id": job_id, "channel_id": channel_id_value},
            )
            await interaction.followup.send("📺 채널 선택 완료. 보고서 목록을 조회합니다...", ephemeral=True)
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────

async def main() -> None:
    try:
        await bot.start(config.discord_bot_token)
    finally:
        if _gateway_client:
            await _gateway_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
