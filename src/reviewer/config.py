"""Application settings, loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr

    github_token: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None
    # Bearer token for the /runs API. Unset means /runs answers 503 (fail closed).
    runs_api_token: SecretStr | None = None

    llm_provider: Literal["openai", "anthropic"] = "openai"
    # Empty/unset means the provider default (see llm/factory.py).
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
