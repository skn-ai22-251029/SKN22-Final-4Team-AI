from enum import Enum
from typing import Optional

from pydantic import BaseModel


class JobStatus(str, Enum):
    DRAFT = "DRAFT"
    SCRIPTING = "SCRIPTING"
    GENERATING = "GENERATING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    APPROVED = "APPROVED"
    WAITING_VIDEO_APPROVAL = "WAITING_VIDEO_APPROVAL"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    ANALYTICS_COLLECTED = "ANALYTICS_COLLECTED"
    FAILED = "FAILED"


class MessengerSource(str, Enum):
    DISCORD = "discord"


class IncomingMessageRequest(BaseModel):
    job_id: str
    messenger_source: MessengerSource
    messenger_user_id: str
    messenger_channel_id: str
    concept_text: str
    ref_image_url: Optional[str] = None
    character_id: str = "default-character"


class SendConfirmRequest(BaseModel):
    job_id: str
    messenger_source: MessengerSource
    messenger_user_id: str
    messenger_channel_id: str
    title: str
    script_summary: str
    preview_url: Optional[str] = None


class ConfirmActionRequest(BaseModel):
    job_id: str
    action: str  # "approved" or "revision_requested"
    revision_note: Optional[str] = None


class SendTextRequest(BaseModel):
    messenger_source: MessengerSource
    messenger_channel_id: str
    text: str


class ReportMessageRequest(BaseModel):
    job_id: str
    messenger_source: MessengerSource
    messenger_user_id: str
    messenger_channel_id: str
    prompt: str
    notebook_id: str = ""
    channel_id: str = ""  # YouTube 채널 ID → notebooklm-service에서 노트북 조회
    character_id: str = "default-character"


class SendReportRequest(BaseModel):
    messenger_source: MessengerSource
    messenger_channel_id: str
    job_id: str
    report_content: str
    file_content_b64: str
    filename: str
    include_video_button: bool = False


class SendVideoPreviewRequest(BaseModel):
    job_id: str
    video_url: str
    channel_id: str
    user_id: str


class VideoActionRequest(BaseModel):
    job_id: str
    action: str  # "approved" | "reject_select" | "reject_step"
    step: Optional[str] = None  # "script" | "tts" | "draft"


class ReportToVideoRequest(BaseModel):
    job_id: str


class ReportSelectRequest(BaseModel):
    job_id: str
    action: str           # "select" | "new"
    report_index: Optional[int] = None   # action="select" 시 필수


class ChannelSelectRequest(BaseModel):
    job_id: str
    channel_id: str     # "UCUpJs89fSBXNolQGOYKn0YQ" (YouTube 채널 ID)
