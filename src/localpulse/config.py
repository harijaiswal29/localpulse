"""Application settings. Never hard-code secrets; everything loads from env / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        protected_namespaces=(),
    )

    app_env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./localpulse.db"
    object_storage_url: str = ""
    object_storage_key: str = ""

    # Model gateway — which model runs each agent is config, not code (spec §13.1).
    llm_gateway: str = "mock"
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    model_content: str = "mock"
    model_router: str = "mock"
    model_insights: str = "mock"
    model_reputation: str = "mock"
    model_engagement: str = "mock"

    whatsapp_bsp_api_key: str = ""
    whatsapp_phone_number_id: str = ""

    gbp_oauth_client_id: str = ""
    gbp_oauth_client_secret: str = ""

    default_monthly_budget_inr: float = 500.0

    def model_map(self) -> dict[str, str]:
        """Task profile -> model id. Agents resolve models through this map only."""
        return {
            "content": self.model_content or "mock",
            "router": self.model_router or "mock",
            "insights": self.model_insights or "mock",
            "reputation": self.model_reputation or "mock",
            "engagement": self.model_engagement or "mock",
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
