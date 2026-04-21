from enum import Enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class JobStatus(str, Enum):
    DRAFT = "DRAFT"
    SCRIPTING = "SCRIPTING"
    GENERATING = "GENERATING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    APPROVED = "APPROVED"
    REPORT_READY = "REPORT_READY"
    WAITING_VIDEO_APPROVAL = "WAITING_VIDEO_APPROVAL"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
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
    job_id: str = ""
    cost_event: dict = {}


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
    include_tts_button: bool = False
    include_video_button: bool = False
    file_url: str = ""


class SendVideoPreviewRequest(BaseModel):
    job_id: str
    video_url: str
    channel_id: str
    user_id: str
    video_filename: str = ""
    heygen_status: str = ""
    heygen_video_id: str = ""
    heygen_avatar_id: str = ""
    heygen_use_avatar_iv_model: bool = False
    heygen_usage_json: dict = {}
    heygen_request_snapshot: dict = {}
    heygen_response_snapshot: dict = {}
    heygen_cost_usd: Optional[float] = None
    heygen_error: str = ""


class SendAudioRequest(BaseModel):
    messenger_source: MessengerSource
    messenger_channel_id: str
    job_id: str
    audio_content_b64: str
    audio_file_path: str = ""
    filename: str
    caption: str = ""
    include_wf12_button: bool = True
    audio_url: str = ""


class TtsActionRequest(BaseModel):
    job_id: str
    action: str  # "select_variant" | "regenerate_batch" | "select_avatar" | "approve_standard" | "approve_hd" | "reject"
    use_avatar_iv_model: bool = False
    batch_id: str = ""
    variant_index: Optional[int] = None
    avatar_index: Optional[int] = None


class VideoActionRequest(BaseModel):
    job_id: str
    action: str  # "approved" | "reject_select" | "reject_step"
    step: Optional[str] = None  # "script" | "tts" | "draft"
    targets: list[str] = []
    publish_title: str = ""


class ReportToVideoRequest(BaseModel):
    job_id: str
    avatar_id: str = ""


class ReportToTtsRequest(BaseModel):
    job_id: str


class ManualGenerateRequest(BaseModel):
    job_id: str = ""
    messenger_user_id: str = ""
    messenger_channel_id: str = ""
    avatar_id: str = ""
    use_avatar_iv_model: bool = False
    prompt: str = ""


class HeygenSmokeTestRequest(BaseModel):
    avatar_id: str = ""


class CharacterAvatarRequest(BaseModel):
    character_id: str
    avatar_id: str = ""


class ListJobsRequest(BaseModel):
    messenger_user_id: str
    messenger_channel_id: str
    limit: int = 5
    purpose: str = "all"  # "all" | "tts" | "heygen"


class ReportSelectRequest(BaseModel):
    job_id: str
    action: str           # "select" | "new"
    report_index: Optional[int] = None   # action="select" 시 필수
    channel_id: str = ""


class ChannelSelectRequest(BaseModel):
    job_id: str
    channel_id: str     # "UCUpJs89fSBXNolQGOYKn0YQ" (YouTube 채널 ID)


class AutoReportRequest(BaseModel):
    channel_id: str
    channel_name: str = ""
    source_url: str = ""
    source_title: str = ""


class Wf13RunJobRequest(BaseModel):
    job_id: str
    seed: int = 1515076784
    avatar_id: str = "b903a1fd1ec846e0ba2e89620bc0aaae"
    use_avatar_iv_model: bool = False
    targets: list[str] = ["youtube"]


class Wf13PreflightRequest(BaseModel):
    channel_ids: list[str] = []


class Wf13RunBatchRequest(BaseModel):
    seed: int = 1515076784
    avatar_id: str = "b903a1fd1ec846e0ba2e89620bc0aaae"
    use_avatar_iv_model: bool = False
    targets: list[str] = ["youtube"]


class SeedLabStartRequest(BaseModel):
    messenger_user_id: str
    messenger_channel_id: str
    seeds: str = ""
    dup: bool = False


class SeedLabRefreshLinkRequest(BaseModel):
    run_id: str
    messenger_user_id: str = ""
    messenger_channel_id: str = ""


class SeedLabProgressRequest(BaseModel):
    run_id: str
    status: str = ""
    stage: str = ""
    eval_location: str = ""
    generated_count: int = 0
    evaluated_count: int = 0
    ready_count: int = 0
    failed_count: int = 0
    eval_failed_count: int = 0
    total_count: int = 0
    runpod_job_count: int = 0
    gpu_active_sample_count: int = 0
    remote_eval_failed_count: int = 0
    remote_eval_last_error: str = ""
    eval_executor_counts: dict = {}
    avg_stage_timings_ms: dict = {}
    last_error: str = ""
    finished_at: str = ""


class CostViewerLinkRequest(BaseModel):
    messenger_user_id: str
    messenger_channel_id: str


class CostEventIngestRequest(BaseModel):
    job_id: str = ""
    topic_text: str = ""
    stage: str
    process: str
    provider: str
    attempt_no: int = 1
    status: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    usage_json: dict = {}
    raw_response_json: dict = {}
    cost_usd: Optional[float] = None
    pricing_kind: str = ""
    pricing_source: str = ""
    api_key_family: str = ""
    subject_type: str = ""
    subject_key: str = ""
    subject_label: str = ""
    error_type: str = ""
    error_message: str = ""
    idempotency_key: str = ""
