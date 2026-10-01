"""Build the configured LLM client from settings."""

from reviewer.config import Settings, get_settings
from reviewer.llm.anthropic import AnthropicClient
from reviewer.llm.base import LLMClient, LLMConfigError
from reviewer.llm.openai import OpenAIClient

DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-5"}


def create_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider = settings.llm_provider
    if provider not in DEFAULT_MODELS:
        raise LLMConfigError(
            f"unknown LLM provider {provider!r}; use one of {sorted(DEFAULT_MODELS)}"
        )

    key = settings.openai_api_key if provider == "openai" else settings.anthropic_api_key
    api_key = key.get_secret_value() if key else ""
    if not api_key:
        env_var = f"{provider.upper()}_API_KEY"
        raise LLMConfigError(f"{env_var} is not set (required when LLM_PROVIDER={provider})")

    model = settings.llm_model or DEFAULT_MODELS[provider]
    client_class = OpenAIClient if provider == "openai" else AnthropicClient
    return client_class(api_key, model, timeout=settings.llm_timeout_seconds)
