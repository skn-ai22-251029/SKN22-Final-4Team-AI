import json
import logging
from typing import Optional

import httpx

from .base import MessengerAdapter

logger = logging.getLogger(__name__)

BASE_URL = "https://discord.com/api/v10"


class DiscordAdapter(MessengerAdapter):

    def __init__(self, token: str, http_client: httpx.AsyncClient) -> None:
        self._token = token
        self._headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }
        self._client = http_client

    async def send_confirm_message(
        self,
        channel_id: str,
        user_id: str,
        job_id: str,
        title: str,
        script_summary: str,
        preview_url: Optional[str],
    ) -> str:
        content = (
            "🎬 **콘텐츠 제작이 완료되었습니다!**\n\n"
            f"📌 제목: {title}\n"
            f"📝 요약: {script_summary}\n\n"
            "승인하시면 각 SNS에 자동 업로드됩니다."
        )
        payload = {
            "content": content,
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "label": "✅ 승인하기",
                            "style": 3,
                            "custom_id": f"approve:{job_id}",
                        },
                        {
                            "type": 2,
                            "label": "✏️ 수정 지시",
                            "style": 2,
                            "custom_id": f"revise:{job_id}",
                        },
                    ],
                }
            ],
        }
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        logger.info("[discord] send_confirm_message job=%s message_id=%s", job_id, message_id)
        return message_id

    async def send_text_message(self, channel_id: str, text: str) -> None:
        payload = {"content": text}
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        logger.info("[discord] send_text_message channel=%s", channel_id)

    async def remove_buttons(
        self,
        channel_id: str,
        message_id: str,
        replacement_text: str,
    ) -> None:
        # 버튼 제거 (컴포넌트를 빈 배열로 PATCH)
        try:
            resp = await self._client.patch(
                f"{BASE_URL}/channels/{channel_id}/messages/{message_id}",
                json={"components": []},
                headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning("[discord] remove_buttons patch failed: %s", e)

        await self.send_text_message(channel_id, replacement_text)
        logger.info("[discord] remove_buttons channel=%s message_id=%s", channel_id, message_id)

    async def send_video_preview(
        self,
        channel_id: str,
        user_id: str,
        job_id: str,
        video_url: str,
    ) -> str:
        content = (
            f"🎬 **영상이 생성되었습니다!**\n\n"
            f"{video_url}\n\n"
            "승인하시면 각 SNS에 자동 업로드됩니다."
        )
        payload = {
            "content": content,
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "label": "✅ 승인",
                            "style": 3,
                            "custom_id": f"video_approve:{job_id}",
                        },
                        {
                            "type": 2,
                            "label": "❌ 반려",
                            "style": 4,
                            "custom_id": f"video_reject:{job_id}",
                        },
                    ],
                }
            ],
        }
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        logger.info("[discord] send_video_preview job=%s message_id=%s", job_id, message_id)
        return message_id

    async def send_reject_step_buttons(self, channel_id: str, job_id: str) -> None:
        payload = {
            "content": "어느 단계로 돌아갈까요?",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "label": "📝 대본 수정",
                            "style": 2,
                            "custom_id": f"video_reject_step:{job_id}:script",
                        },
                        {
                            "type": 2,
                            "label": "🔊 TTS 재생성",
                            "style": 2,
                            "custom_id": f"video_reject_step:{job_id}:tts",
                        },
                        {
                            "type": 2,
                            "label": "🔄 처음부터",
                            "style": 4,
                            "custom_id": f"video_reject_step:{job_id}:draft",
                        },
                    ],
                }
            ],
        }
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        logger.info("[discord] send_reject_step_buttons job=%s channel=%s", job_id, channel_id)

    async def send_report_list(
        self,
        channel_id: str,
        job_id: str,
        reports: list[str],
    ) -> None:
        """기존 보고서 선택 버튼 + '새로 생성' 버튼을 Discord로 전송한다.
        Discord 제한: 5버튼/행 × 5행 = 최대 25개. 보고서는 최대 24개."""
        all_buttons = []
        for i, title in enumerate(reports[:24]):
            all_buttons.append({
                "type": 2,
                "label": f"{i + 1}. {title[:60]}",
                "style": 2,  # Secondary
                "custom_id": f"select_report:{job_id}:{i}",
            })
        all_buttons.append({
            "type": 2,
            "label": "🆕 새로 생성",
            "style": 1,  # Primary
            "custom_id": f"new_report:{job_id}",
        })

        # 5개씩 ActionRow로 묶음
        rows = []
        for chunk_start in range(0, len(all_buttons), 5):
            rows.append({
                "type": 1,
                "components": all_buttons[chunk_start:chunk_start + 5],
            })

        payload = {
            "content": "📋 **기존 보고서를 선택하거나 새로 생성하세요.**",
            "components": rows,
        }
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        logger.info("[discord] send_report_list job=%s reports=%d", job_id, len(reports))

    async def send_channel_list(
        self, channel_id: str, job_id: str, channels: list[dict]
    ) -> None:
        """채널 선택 버튼 목록을 Discord로 전송한다.
        custom_id 형식: select_channel:{job_id}:{channel_id}"""
        buttons = [
            {
                "type": 2,
                "style": 2,
                "label": ch["name"][:80],
                "custom_id": f"select_channel:{job_id}:{ch['id']}",
            }
            for ch in channels[:25]
        ]

        rows = []
        for chunk_start in range(0, len(buttons), 5):
            rows.append({
                "type": 1,
                "components": buttons[chunk_start:chunk_start + 5],
            })

        payload = {
            "content": "📺 채널을 선택하세요.",
            "components": rows,
        }
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        logger.info("[discord] send_channel_list job=%s channels=%d", job_id, len(channels))

    async def send_file_message(
        self,
        channel_id: str,
        text: str,
        file_bytes: bytes,
        filename: str,
        include_video_button: bool = False,
        job_id: str = "",
    ) -> None:
        components = []
        if include_video_button and job_id:
            components = [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "label": "🎬 영상으로 제작",
                            "style": 1,
                            "custom_id": f"report_to_video:{job_id}",
                        }
                    ],
                }
            ]
        payload_json = json.dumps({"content": text, "components": components})
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {self._token}"},
            files={"files[0]": (filename, file_bytes, "text/markdown")},
            data={"payload_json": payload_json},
        )
        resp.raise_for_status()
        logger.info("[discord] send_file_message channel=%s filename=%s", channel_id, filename)
