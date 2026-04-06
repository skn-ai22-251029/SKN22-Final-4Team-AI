from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gateway_port: int = 8080
    gateway_internal_secret: str

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "ai_influencer"
    postgres_user: str = "aiuser"
    postgres_password: str

    discord_bot_token: str = ""
    discord_allowed_channel_ids: str = ""
    auto_report_discord_delivery_enabled: bool = True
    auto_report_max_attempts_per_day: int = 3
    auto_report_stale_minutes: int = 45
    openai_api_key: str = ""
    script_rewrite_model: str = "gpt-5.4"

    n8n_wf01_webhook_url: str = "http://n8n:5678/webhook/Mt5nwvystMhfO1nl/webhook/wf-01-input"
    n8n_wf05_webhook_url: str = "http://n8n:5678/webhook/gD9A0qy9MxY8g0T6/webhook/wf-05-confirm"
    n8n_wf06_webhook_url: str = "http://n8n:5678/webhook/QSrXdaRpKosyZIj3/webhook/wf-06-report"
    n8n_wf11_webhook_url: str = "http://n8n:5678/webhook/Wv5SdSdlPLwNzeqF/webhook/wf-11-tts-generate"
    n8n_wf12_webhook_url: str = "http://n8n:5678/webhook/WF12HeygenV2Run/webhook/wf-12-heygen-generate-v2"
    n8n_wf08_webhook_url: str = "http://n8n:5678/webhook/uLRW8JT5UitrhCC9/webhook/wf-08-sns-upload"

    notebooklm_service_url: str = "http://notebooklm-service:8090"
    topic_channels: str = ""
    tts_api_url: str = ""
    tts_ref_audio_path: str = ""
    tts_prompt_text: str = ""
    tts_text_lang: str = "ko"
    tts_prompt_lang: str = "ko"
    tts_top_k: int = 20
    tts_sample_steps: int = 32
    tts_super_sampling: bool = True
    tts_fragment_interval: float = 0.4
    tts_fixed_seeds: str = ""
    tts_fixed_seeds_by_channel: str = ""
    heygen_api_key: str = ""
    heygen_avatar_id: str = ""
    heygen_api_base_url: str = "https://api.heygen.com"
    heygen_upload_base_url: str = "https://upload.heygen.com"
    heygen_video_width: int = 1080
    heygen_video_height: int = 1920
    heygen_caption_enabled: bool = False
    heygen_speed: float = 1.3
    heygen_poll_interval_seconds: int = 10
    heygen_max_wait_seconds: int = 900
    heygen_mock_enabled: bool = False
    heygen_mock_video_url: str = "https://samplelib.com/lib/preview/mp4/sample-5s.mp4"
    heygen_pipeline_service_url: str = "http://heygen-pipeline-service:8100"
    heygen_pipeline_service_timeout_seconds: float = 120.0

    # Cross-account S3 (AssumeRole)
    media_s3_bucket: str = ""
    media_s3_region: str = "ap-northeast-2"
    media_s3_role_arn: str = ""
    media_s3_external_id: str = ""
    media_s3_role_session_name: str = "ai-influencer-media-session"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    media_presign_expires_seconds: int = 86400
    media_max_discord_file_bytes: int = 10 * 1024 * 1024
    media_s3_prefix_reports: str = "subtitle"
    media_s3_prefix_logs: str = "logs"
    media_s3_prefix_scripts: str = "scripts"
    media_s3_prefix_tts: str = "tts"
    media_s3_prefix_videos: str = "videos"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
