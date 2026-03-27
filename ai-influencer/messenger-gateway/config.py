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

    n8n_wf01_webhook_url: str = "http://n8n:5678/webhook/wf-01-input"
    n8n_wf05_webhook_url: str = "http://n8n:5678/webhook/wf-05-confirm"
    n8n_wf06_webhook_url: str = "http://n8n:5678/webhook/wf-06-report"
    n8n_wf11_webhook_url: str = "http://n8n:5678/webhook/wf-11-tts-generate"
    n8n_wf12_webhook_url: str = "http://n8n:5678/webhook/wf-12-heygen-generate"
    n8n_wf08_webhook_url: str = "http://n8n:5678/webhook/wf-08-sns-upload"

    notebooklm_service_url: str = "http://notebooklm-service:8090"
    topic_channels: str = ""

    # Cross-account S3 (AssumeRole)
    media_s3_bucket: str = ""
    media_s3_region: str = "ap-northeast-2"
    media_s3_role_arn: str = ""
    media_s3_external_id: str = ""
    media_s3_role_session_name: str = "ai-influencer-media-session"
    media_presign_expires_seconds: int = 86400
    media_max_discord_file_bytes: int = 10 * 1024 * 1024
    media_s3_prefix_reports: str = "reports"
    media_s3_prefix_tts: str = "tts"
    media_s3_prefix_videos: str = "videos"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
