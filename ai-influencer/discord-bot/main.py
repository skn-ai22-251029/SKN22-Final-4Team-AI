import asyncio
import logging
import uuid
from typing import Optional

import discord
import httpx
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
    gateway_url: str = "http://messenger-gateway:8080"
    gateway_internal_secret: str

    class Config:
        env_file = ".env"
        case_sensitive = False


config = Settings()
ALLOWED_USER_IDS: set[str] = {
    uid.strip() for uid in config.discord_allowed_user_ids.split(",") if uid.strip()
}
ALLOWED_CHANNEL_IDS: set[str] = {
    cid.strip() for cid in config.discord_allowed_channel_ids.split(",") if cid.strip()
}


# ─────────────────────────────────────────
# Gateway 클라이언트 헬퍼
# ─────────────────────────────────────────

_gateway_client: Optional[httpx.AsyncClient] = None


def get_gateway_client() -> httpx.AsyncClient:
    global _gateway_client
    if _gateway_client is None:
        _gateway_client = httpx.AsyncClient(
            base_url=config.gateway_url,
            headers={"X-Internal-Secret": config.gateway_internal_secret},
            timeout=30.0,
        )
    return _gateway_client


async def gateway_call(path: str, payload: dict) -> None:
    try:
        resp = await get_gateway_client().post(path, json=payload)
        resp.raise_for_status()
    except Exception as e:
        logger.error("[discord] gateway_call %s failed: %s", path, e)
        raise


# ─────────────────────────────────────────
# 인메모리 수정 대기 상태 (pending_store)
# ─────────────────────────────────────────

revision_pending: dict[str, str] = {}  # user_id -> job_id


# ─────────────────────────────────────────
# Discord Bot
# ─────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    await bot.tree.sync()
    logger.info("[discord] bot online: %s / slash commands synced", bot.user)


@bot.event
async def on_message(message: discord.Message) -> None:
    # 봇 자신의 메시지 무시
    if message.author.bot:
        return

    # 허용 채널 확인
    if ALLOWED_CHANNEL_IDS and str(message.channel.id) not in ALLOWED_CHANNEL_IDS:
        return

    user_id = str(message.author.id)

    # 허용 사용자 확인
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    # 수정 지시 텍스트 대기 중인 경우에만 처리
    if user_id in revision_pending:
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

    if ALLOWED_CHANNEL_IDS and str(interaction.channel_id) not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("이 채널에서는 사용할 수 없습니다.", ephemeral=True)
        return

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
        return

    await interaction.response.defer()

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
    prompt: str,
) -> None:
    user_id = str(interaction.user.id)

    if ALLOWED_CHANNEL_IDS and str(interaction.channel_id) not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("이 채널에서는 사용할 수 없습니다.", ephemeral=True)
        return

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
        return

    await interaction.response.defer()

    job_id = str(uuid.uuid4())
    try:
        await gateway_call(
            "/internal/report-message",
            {
                "job_id": job_id,
                "messenger_source": "discord",
                "messenger_user_id": user_id,
                "messenger_channel_id": str(interaction.channel_id),
                "prompt": _REPORT_SYSTEM_PROMPT + prompt,
                "notebook_id": "",
                "channel_id": "",
                "character_id": "default-character",
            },
        )
        await interaction.followup.send(
            f"📊 요청 접수! 채널을 선택하면 보고서를 가져옵니다. ⏳\nJob ID: `{job_id[:8]}`"
        )
    except Exception:
        await interaction.followup.send("보고서 요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    # 버튼 클릭(컴포넌트 인터랙션)만 처리
    if interaction.type != discord.InteractionType.component:
        return

    custom_id: str = interaction.data.get("custom_id", "")
    if ":" not in custom_id:
        await interaction.response.send_message("잘못된 요청입니다.", ephemeral=True)
        return

    parts = custom_id.split(":")
    action = parts[0]
    # video_reject_step: video_reject_step:{job_id}:{step}
    # select_report:     select_report:{job_id}:{index}
    # select_channel:    select_channel:{job_id}:{channel_id}
    step = None
    report_index = None
    channel_id_value = None
    if action == "video_reject_step" and len(parts) >= 3:
        job_id = parts[1]
        step = parts[2]
    elif action == "select_report" and len(parts) >= 3:
        job_id = parts[1]
        report_index = int(parts[2])
    elif action == "select_channel" and len(parts) >= 3:
        job_id = parts[1]
        channel_id_value = ":".join(parts[2:])
    else:
        job_id = ":".join(parts[1:])
    user_id = str(interaction.user.id)

    # 허용 채널 확인
    if ALLOWED_CHANNEL_IDS and str(interaction.channel_id) not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("이 채널에서는 사용할 수 없습니다.", ephemeral=True)
        return

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
        return

    # Discord 3초 응답 제한 — 먼저 defer
    await interaction.response.defer()

    if action == "approve":
        try:
            await gateway_call(
                "/internal/confirm-action",
                {"job_id": job_id, "action": "approved"},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "revise":
        revision_pending[user_id] = job_id
        await interaction.channel.send("✏️ 어떤 점을 수정할까요? 구체적으로 입력해주세요.")

    elif action == "video_approve":
        try:
            await gateway_call(
                "/internal/video-action",
                {"job_id": job_id, "action": "approved"},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "video_reject":
        try:
            await gateway_call(
                "/internal/video-action",
                {"job_id": job_id, "action": "reject_select"},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "video_reject_step":
        try:
            await gateway_call(
                "/internal/video-action",
                {"job_id": job_id, "action": "reject_step", "step": step},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "report_to_video":
        try:
            await gateway_call(
                "/internal/report-to-video",
                {"job_id": job_id},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "select_report":
        try:
            await gateway_call(
                "/internal/report-select",
                {"job_id": job_id, "action": "select", "report_index": report_index},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "new_report":
        try:
            await gateway_call(
                "/internal/report-select",
                {"job_id": job_id, "action": "new"},
            )
        except Exception as e:
            await interaction.channel.send(f"오류가 발생했습니다: {e}")

    elif action == "select_channel":
        try:
            await gateway_call(
                "/internal/channel-select",
                {"job_id": job_id, "channel_id": channel_id_value},
            )
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
