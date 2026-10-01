from pydantic import BaseModel

from reviewer.db.models import LLMCall
from reviewer.llm.base import LLMError, LLMOutputError, LLMUsage
from reviewer.llm.fake import FakeLLM
from reviewer.llm.records import MAX_ERROR_LENGTH, llm_call_row


class Verdict(BaseModel):
    summary: str


def test_row_from_successful_result():
    result = FakeLLM([Verdict(summary="ok")]).complete_structured("s", "u", Verdict)

    assert llm_call_row("code_review", result) == {
        "node": "code_review",
        "provider": "fake",
        "model": "fake-model",
        "input_tokens": 10,
        "output_tokens": 5,
        "latency_ms": 0,
        "retries": 0,
        "error": None,
    }


def test_row_from_failed_call_keeps_spent_usage():
    exc = LLMOutputError("model output failed validation after one repair: summary: Field required")
    exc.usage = LLMUsage(input_tokens=50, output_tokens=11, latency_ms=1234, retries=2)
    exc.provider, exc.model = "openai", "gpt-4o-mini"

    row = llm_call_row("test_analysis", exc)

    assert row["provider"] == "openai"
    assert (row["input_tokens"], row["output_tokens"]) == (50, 11)
    assert (row["latency_ms"], row["retries"]) == (1234, 2)
    assert row["error"].startswith("LLMOutputError: model output failed validation")


def test_row_from_error_without_usage_uses_zeros():
    row = llm_call_row("final_review", LLMError("boom"))

    assert row["input_tokens"] == row["output_tokens"] == row["latency_ms"] == row["retries"] == 0
    assert row["provider"] == row["model"] == "unknown"


def test_long_errors_are_truncated():
    row = llm_call_row("n", LLMError("x" * 10_000))

    assert len(row["error"]) == MAX_ERROR_LENGTH


def test_row_keys_are_columns_of_llm_calls_table():
    """Keeps the helper in sync with the schema: run_id/created_at are the only ones left out."""
    row = llm_call_row("n", LLMError("boom"))
    columns = {c.name for c in LLMCall.__table__.columns}

    assert set(row) <= columns
    assert columns - set(row) == {"id", "run_id", "created_at"}
