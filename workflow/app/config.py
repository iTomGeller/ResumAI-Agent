from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    java_backend_url: str = "http://ai-resume-backend:8080"
    workflow_internal_token: str = "change-me"

    skills_path: str = "/app/skills"


settings = Settings()


def normalized_deepseek_base_url(raw_url: str | None = None) -> str:
    url = (raw_url or settings.deepseek_api_url or "https://api.deepseek.com/v1").strip()
    if "/chat/completions" in url:
        url = url.replace("/chat/completions", "/v1")
    return url.rstrip("/")
