import pytest

from reviewer.config import Settings
from reviewer.llm.anthropic import AnthropicClient
from reviewer.llm.base import LLMConfigError
from reviewer.llm.factory import create_llm_client
from reviewer.llm.openai import OpenAIClient


def settings(**overrides) -> Settings:
    return Settings(database_url="sqlite://", **overrides)


def test_openai_provider_with_default_model():
    client = create_llm_client(settings(llm_provider="openai", openai_api_key="sk-x"))

    assert isinstance(client, OpenAIClient)
    assert client.provider == "openai"
    assert client.model == "gpt-4o-mini"


def test_anthropic_provider_with_default_model():
    client = create_llm_client(settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x"))

    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-sonnet-5"


def test_model_override():
    client = create_llm_client(
        settings(llm_provider="openai", openai_api_key="sk-x", llm_model="gpt-4.1")
    )

    assert client.model == "gpt-4.1"


def test_empty_model_falls_back_to_provider_default():
    client = create_llm_client(
        settings(llm_provider="anthropic", anthropic_api_key="k", llm_model="")
    )

    assert client.model == "claude-sonnet-5"


@pytest.mark.parametrize(
    ("provider", "env_var"), [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")]
)
def test_missing_key_fails_with_clear_error(provider, env_var):
    with pytest.raises(LLMConfigError, match=env_var):
        create_llm_client(settings(llm_provider=provider))


def test_only_the_selected_providers_key_counts():
    with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
        create_llm_client(settings(llm_provider="openai", anthropic_api_key="sk-ant-x"))


def test_empty_key_counts_as_missing():
    with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
        create_llm_client(settings(llm_provider="openai", openai_api_key=""))


def test_unknown_provider():
    bad = settings(openai_api_key="sk-x").model_copy(update={"llm_provider": "nope"})

    with pytest.raises(LLMConfigError, match="unknown LLM provider 'nope'"):
        create_llm_client(bad)


def test_reads_settings_from_environment(monkeypatch):
    from reviewer.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    monkeypatch.setenv("LLM_MODEL", "")  # what `LLM_MODEL=` in .env produces
    get_settings.cache_clear()

    assert create_llm_client().provider == "anthropic"


def test_error_message_never_contains_the_key():
    with pytest.raises(LLMConfigError) as exc_info:
        create_llm_client(settings(llm_provider="openai", anthropic_api_key="sk-ant-secret"))

    assert "sk-ant-secret" not in str(exc_info.value)
