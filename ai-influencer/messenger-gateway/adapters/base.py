from abc import ABC, abstractmethod
from typing import Optional


class MessengerAdapter(ABC):

    @abstractmethod
    async def send_confirm_message(
        self,
        channel_id: str,
        user_id: str,
        job_id: str,
        title: str,
        script_summary: str,
        preview_url: Optional[str],
    ) -> str:
        """컨펌 메시지(인라인 버튼 포함)를 전송하고 confirm_message_id를 반환한다."""

    @abstractmethod
    async def send_text_message(
        self,
        channel_id: str,
        text: str,
    ) -> None:
        """일반 텍스트 메시지를 전송한다."""

    @abstractmethod
    async def remove_buttons(
        self,
        channel_id: str,
        message_id: str,
        replacement_text: str,
    ) -> None:
        """컨펌 메시지의 버튼을 제거하고 replacement_text를 전송한다."""
