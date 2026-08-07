from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_quality_model: str = "deepseek-v4-pro"

    java_backend_url: str = "http://ai-resume-backend:8080"
    workflow_internal_token: str = "change-me"
    workflow_build_version: str = "dev"
    # Persist exact workflow Agent -> LLM request/response envelopes when
    # explicitly enabled. Prompts contain candidate data, so the default is
    # intentionally off; the Java persistence boundary performs redaction.
    context_audit_enabled: bool = False

    # The production compose file enables LangGraph explicitly. Keeping the
    # local default off preserves dependency-light unit tests and provides the
    # requested one-switch rollback to the legacy coordinator loop.
    langgraph_runtime_enabled: bool = False
    langgraph_checkpoint_dsn: str = ""

    skills_path: str = "/app/skills"
    mcp_config_path: str = ""  # empty → resolve via mcp_registry fallbacks

    # Redis semantic cache (parse_resume / JD analysis / coordinator plans).
    redis_url: str = "redis://:@resumai-redis:6379/1"
    cache_enabled: bool = True

    # OpenRouter embeddings (shared model/dimension with the Java side).
    openrouter_api_key: str = ""
    embedding_model: str = "openai/text-embedding-3-small"


settings = Settings()


def normalized_deepseek_base_url(raw_url: str | None = None) -> str:
    url = (raw_url or settings.deepseek_api_url or "https://api.deepseek.com/v1").strip()
    if "/chat/completions" in url:
        url = url.replace("/chat/completions", "/v1")
    return url.rstrip("/")
