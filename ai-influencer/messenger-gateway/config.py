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
    publish_stale_minutes: int = 10
    openai_fallback_api_key: str = ""
    openai_api_key_rewrite: str = ""
    script_rewrite_model: str = "gpt-5.4"
    script_rewrite_topic_keyword_guard_enabled: bool = False

    n8n_wf01_webhook_url: str = "http://n8n:5678/webhook/Mt5nwvystMhfO1nl/webhook/wf-01-input"
    n8n_wf05_webhook_url: str = "http://n8n:5678/webhook/gD9A0qy9MxY8g0T6/webhook/wf-05-confirm"
    n8n_wf06_webhook_url: str = "http://n8n:5678/webhook/QSrXdaRpKosyZIj3/webhook/wf-06-report"
    n8n_wf11_webhook_url: str = "http://n8n:5678/webhook/Wv5SdSdlPLwNzeqF/webhook/wf-11-tts-generate"
    n8n_wf12_webhook_url: str = "http://n8n:5678/webhook/WF12HeygenV2Run/webhook/wf-12-heygen-generate-v2"
    n8n_wf08_webhook_url: str = "http://n8n:5678/webhook/uLRW8JT5UitrhCC9/webhook/wf-08-sns-upload"

    notebooklm_service_url: str = "http://notebooklm-service:8090"
    topic_channels: str = ""
    tts_router_url: str = "http://tts-router-service:8300"
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
    tts_opening_audio_path: str = ""
    tts_ending_audio_path: str = ""
    tts_opening_gap_seconds: float = 0.5
    tts_ending_gap_seconds: float = 0.5
    tts_concat_retries: int = 2
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
    sns_publisher_service_url: str = "http://sns-publisher-service:8200"
    seedlab_service_url: str = "http://seed-lab-service:8300"
    seedlab_signing_secret: str = ""
    seedlab_link_ttl_seconds: int = 604800
    seedlab_public_base_url: str = ""

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
    media_s3_prefix_srt: str = "srt"
    media_s3_prefix_videos_with_subtitle: str = "Video_with_Subtitle"

    # Cost tracking
    cost_viewer_basic_user: str = ""
    cost_viewer_basic_password: str = ""
    cost_viewer_public_base_url: str = ""
    cost_viewer_link_ttl_seconds: int = 604800
    cost_usd_krw_rate: float = 1350.0
    cost_default_list_limit: int = 50
    cost_max_list_limit: int = 200
    script_rewrite_input_cost_usd_per_1m: float = 0.0
    script_rewrite_output_cost_usd_per_1m: float = 0.0
    tts_cost_usd_per_1k_chars: float = 0.0
    heygen_fallback_cost_usd_per_video: float = 0.0
    aws_daily_fixed_usd: float = 0.0
    runpod_daily_fixed_usd: float = 0.0
    runpod_gpu_cost_usd_per_hour: float = 0.0  # RTX 2000 Ada Pod 단가 (예: 0.24)

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
