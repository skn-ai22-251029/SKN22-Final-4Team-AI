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

    n8n_wf01_webhook_url: str = "http://n8n:5678/webhook/wf-01-input"
    n8n_wf05_webhook_url: str = "http://n8n:5678/webhook/wf-05-confirm"
    n8n_wf06_webhook_url: str = "http://n8n:5678/webhook/wf-06-report"
    n8n_wf07_webhook_url: str = "http://n8n:5678/webhook/wf-07-tts-heygen"
    n8n_wf08_webhook_url: str = "http://n8n:5678/webhook/wf-08-sns-upload"

    notebooklm_service_url: str = "http://notebooklm-service:8090"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
