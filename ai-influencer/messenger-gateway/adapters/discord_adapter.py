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

    async def send_seedlab_progress_message(self, channel_id: str, text: str) -> str:
        payload = {"content": text}
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        logger.info("[discord] send_seedlab_progress_message channel=%s message_id=%s", channel_id, message_id)
        return message_id

    async def edit_seedlab_progress_message(self, channel_id: str, message_id: str, text: str) -> None:
        payload = {"content": text}
        resp = await self._client.patch(
            f"{BASE_URL}/channels/{channel_id}/messages/{message_id}",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        logger.info("[discord] edit_seedlab_progress_message channel=%s message_id=%s", channel_id, message_id)

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

    async def clear_message_components(
        self,
        channel_id: str,
        message_id: str,
        content: Optional[str] = None,
    ) -> None:
        payload: dict[str, object] = {"components": []}
        if content is not None:
            payload["content"] = content
        resp = await self._client.patch(
            f"{BASE_URL}/channels/{channel_id}/messages/{message_id}",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        logger.info("[discord] clear_message_components channel=%s message_id=%s", channel_id, message_id)

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
            "승인 후 업로드할 플랫폼을 선택합니다."
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
        selected_channel_id: str,
    ) -> None:
        """기존 보고서 선택 버튼 + '새로 생성' 버튼을 Discord로 전송한다.
        Discord 제한: 5버튼/행 × 5행 = 최대 25개. 보고서는 최대 24개."""
        all_buttons = []
        for i, title in enumerate(reports[:24]):
            all_buttons.append({
                "type": 2,
                "label": f"{i + 1}. {title[:60]}",
                "style": 2,  # Secondary
                "custom_id": f"select_report:{job_id}:{selected_channel_id}:{i}",
            })
        all_buttons.append({
            "type": 2,
            "label": "🆕 새로 생성",
            "style": 1,  # Primary
            "custom_id": f"new_report:{job_id}:{selected_channel_id}",
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

    async def send_report_recovery_actions(
        self,
        channel_id: str,
        job_id: str,
        selected_channel_id: str,
        reason_text: str,
        *,
        include_retry: bool = True,
    ) -> None:
        """보고서 목록 조회 실패/빈 목록 상황에서 재시도/새 생성 버튼을 전송한다."""
        buttons = []
        if include_retry:
            buttons.append(
                {
                    "type": 2,
                    "label": "🔄 다시 조회",
                    "style": 2,
                    "custom_id": f"select_channel:{job_id}:{selected_channel_id}",
                }
            )
        buttons.append(
            {
                "type": 2,
                "label": "🆕 새로 생성",
                "style": 1,
                "custom_id": f"new_report:{job_id}:{selected_channel_id}",
            }
        )

        payload = {
            "content": reason_text,
            "components": [{"type": 1, "components": buttons}],
        }
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        logger.info(
            "[discord] send_report_recovery_actions job=%s include_retry=%s",
            job_id,
            include_retry,
        )

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
        include_tts_button: bool = False,
        include_video_button: bool = False,
        job_id: str = "",
    ) -> None:
        components = []
        action_buttons = []
        if include_tts_button and job_id:
            action_buttons.append(
                {
                    "type": 2,
                    "label": "🔊 TTS만 제작",
                    "style": 2,
                    "custom_id": f"report_to_tts:{job_id}",
                }
            )
        if include_video_button and job_id:
            action_buttons.append(
                {
                    "type": 2,
                    "label": "🎬 영상으로 제작",
                    "style": 1,
                    "custom_id": f"report_to_video:{job_id}",
                }
            )
        if action_buttons:
            components = [{"type": 1, "components": action_buttons}]
        payload_json = json.dumps({"content": text, "components": components})
        content_type = "text/plain" if filename.lower().endswith(".txt") else "text/markdown"
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {self._token}"},
            files={"files[0]": (filename, file_bytes, content_type)},
            data={"payload_json": payload_json},
        )
        resp.raise_for_status()
        logger.info("[discord] send_file_message channel=%s filename=%s", channel_id, filename)

    async def send_report_link_message(
        self,
        channel_id: str,
        text: str,
        report_url: str,
        include_tts_button: bool = False,
        include_video_button: bool = False,
        job_id: str = "",
    ) -> str:
        prefix = (text or "").strip() or "📄 보고서 생성이 완료되었습니다."
        suffix = f"\n\n📎 보고서 링크(24시간 유효):\n{report_url}"
        max_prefix_len = max(0, 1900 - len(suffix))
        if len(prefix) > max_prefix_len:
            prefix = prefix[:max_prefix_len]
        content = f"{prefix}{suffix}"
        components = []
        action_buttons = []
        if include_tts_button and job_id:
            action_buttons.append(
                {
                    "type": 2,
                    "label": "🔊 TTS만 제작",
                    "style": 2,
                    "custom_id": f"report_to_tts:{job_id}",
                }
            )
        if include_video_button and job_id:
            action_buttons.append(
                {
                    "type": 2,
                    "label": "🎬 영상으로 제작",
                    "style": 1,
                    "custom_id": f"report_to_video:{job_id}",
                }
            )
        if action_buttons:
            components = [{"type": 1, "components": action_buttons}]
        payload = {"content": content, "components": components}
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        logger.info("[discord] send_report_link_message channel=%s message_id=%s", channel_id, message_id)
        return message_id

    async def send_tts_audio_message(
        self,
        channel_id: str,
        job_id: str,
        caption: str,
        audio_bytes: bytes,
        filename: str,
        include_wf12_button: bool = True,
        selected_avatar_index: Optional[int] = None,
        avatar_options: Optional[list[dict[str, object]]] = None,
    ) -> tuple[str, str]:
        components = self._build_tts_approval_components(
            job_id=job_id,
            include_wf12_button=include_wf12_button,
            selected_avatar_index=selected_avatar_index,
            avatar_options=avatar_options,
        )

        payload_json = json.dumps({"content": caption, "components": components})
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {self._token}"},
            files={"files[0]": (filename, audio_bytes, "audio/wav")},
            data={"payload_json": payload_json},
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        attachment_url = ""
        attachments = data.get("attachments") or []
        if attachments and isinstance(attachments[0], dict):
            attachment_url = str(attachments[0].get("url") or "")
        logger.info("[discord] send_tts_audio_message job=%s message_id=%s", job_id, message_id)
        return message_id, attachment_url

    async def send_tts_variant_control_message(
        self,
        channel_id: str,
        job_id: str,
        batch_id: str,
        caption: str,
    ) -> str:
        payload = {
            "content": caption,
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "label": "🔁 다시 생성",
                            "style": 2,
                            "custom_id": f"tts_regenerate:{job_id}:{batch_id}",
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
        logger.info("[discord] send_tts_variant_control_message job=%s batch_id=%s message_id=%s", job_id, batch_id, message_id)
        return message_id

    async def send_tts_variant_audio_message(
        self,
        channel_id: str,
        job_id: str,
        batch_id: str,
        variant_index: int,
        caption: str,
        audio_bytes: bytes,
        filename: str,
    ) -> tuple[str, str]:
        payload_json = json.dumps(
            {
                "content": caption,
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "label": "✅ 이 버전 선택",
                                "style": 3,
                                "custom_id": f"tts_select:{job_id}:{batch_id}:{variant_index}",
                            },
                        ],
                    }
                ],
            }
        )
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {self._token}"},
            files={"files[0]": (filename, audio_bytes, "audio/wav")},
            data={"payload_json": payload_json},
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        attachment_url = ""
        attachments = data.get("attachments") or []
        if attachments and isinstance(attachments[0], dict):
            attachment_url = str(attachments[0].get("url") or "")
        logger.info(
            "[discord] send_tts_variant_audio_message job=%s batch_id=%s variant=%s message_id=%s",
            job_id,
            batch_id,
            variant_index,
            message_id,
        )
        return message_id, attachment_url

    async def send_tts_variant_link_message(
        self,
        channel_id: str,
        job_id: str,
        batch_id: str,
        variant_index: int,
        caption: str,
        audio_url: str,
    ) -> str:
        payload = {
            "content": f"{caption}\n\n📎 TTS 링크(24시간 유효):\n{audio_url}",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "label": "✅ 이 버전 선택",
                            "style": 3,
                            "custom_id": f"tts_select:{job_id}:{batch_id}:{variant_index}",
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
        logger.info(
            "[discord] send_tts_variant_link_message job=%s batch_id=%s variant=%s message_id=%s",
            job_id,
            batch_id,
            variant_index,
            message_id,
        )
        return message_id

    async def send_tts_approval_message(
        self,
        channel_id: str,
        job_id: str,
        caption: str,
        selected_avatar_index: Optional[int] = None,
        avatar_options: Optional[list[dict[str, object]]] = None,
    ) -> str:
        payload = {
            "content": caption,
            "components": self._build_tts_approval_components(
                job_id=job_id,
                include_wf12_button=True,
                selected_avatar_index=selected_avatar_index,
                avatar_options=avatar_options,
            ),
        }
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        logger.info("[discord] send_tts_approval_message job=%s message_id=%s", job_id, message_id)
        return message_id

    async def send_tts_link_message(
        self,
        channel_id: str,
        job_id: str,
        caption: str,
        audio_url: str,
        include_wf12_button: bool = True,
        selected_avatar_index: Optional[int] = None,
        avatar_options: Optional[list[dict[str, object]]] = None,
    ) -> str:
        components = self._build_tts_approval_components(
            job_id=job_id,
            include_wf12_button=include_wf12_button,
            selected_avatar_index=selected_avatar_index,
            avatar_options=avatar_options,
        )
        prefix = (caption or "").strip() or "🔊 TTS 완료본입니다. 일반 승인 또는 고화질 승인을 선택한 뒤 최종 확인을 진행하세요."
        suffix = f"\n\n📎 TTS 링크(24시간 유효):\n{audio_url}"
        max_prefix_len = max(0, 1900 - len(suffix))
        if len(prefix) > max_prefix_len:
            prefix = prefix[:max_prefix_len]
        content = f"{prefix}{suffix}"
        payload = {"content": content, "components": components}
        resp = await self._client.post(
            f"{BASE_URL}/channels/{channel_id}/messages",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data["id"])
        logger.info("[discord] send_tts_link_message job=%s message_id=%s", job_id, message_id)
        return message_id

    def _build_tts_approval_components(
        self,
        *,
        job_id: str,
        include_wf12_button: bool,
        selected_avatar_index: Optional[int],
        avatar_options: Optional[list[dict[str, object]]],
    ) -> list[dict]:
        if not include_wf12_button:
            return []
        avatar_buttons = []
        normalized_index = selected_avatar_index if isinstance(selected_avatar_index, int) else None
        options = avatar_options or []
        for option in options:
            idx = int(option.get("index", -1))
            label = str(option.get("label") or "").strip()
            if idx < 0 or not label:
                continue
            is_selected = normalized_index == idx
            avatar_buttons.append(
                {
                    "type": 2,
                    "label": f"{'✅ ' if is_selected else ''}{label}",
                    "style": 3 if is_selected else 2,
                    "custom_id": f"tts_avatar_pick:{job_id}:{idx}",
                }
            )
        rows: list[dict] = []
        for start in range(0, len(avatar_buttons), 5):
            row_buttons = avatar_buttons[start : start + 5]
            if not row_buttons:
                continue
            rows.append(
                {
                    "type": 1,
                    "components": row_buttons,
                }
            )
        rows.append(
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "label": "✅ 일반 승인",
                        "style": 3,
                        "custom_id": f"tts_approve_standard:{job_id}",
                    },
                    {
                        "type": 2,
                        "label": "💎 고화질 승인",
                        "style": 2,
                        "custom_id": f"tts_approve_hd:{job_id}",
                    },
                    {
                        "type": 2,
                        "label": "❌ 반려",
                        "style": 4,
                        "custom_id": f"tts_reject:{job_id}",
                    },
                ],
            }
        )
        return rows
