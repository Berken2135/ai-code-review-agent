import pytest
from pydantic import ValidationError

from reviewer.config import Settings, get_settings


def test_defaults_and_secret_masking(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.llm_provider == "openai"
    assert settings.anthropic_api_key is None
    assert "sk-test-123" not in repr(settings)
    assert settings.openai_api_key.get_secret_value() == "sk-test-123"


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL")

    with pytest.raises(ValidationError):
        Settings()


def test_invalid_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nope")

    with pytest.raises(ValidationError):
        Settings()
