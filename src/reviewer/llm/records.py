"""Turn an LLM outcome into a row for the `llm_calls` table (not wired to the DB yet)."""

from typing import Any

from reviewer.llm.base import LLMError, LLMResult, LLMUsage

MAX_ERROR_LENGTH = 2000


def llm_call_row(node: str, outcome: LLMResult[Any] | LLMError) -> dict[str, Any]:
    """Column values for `LLMCall`, minus `run_id` and `created_at` (the caller adds those).

    A failed call still reports the tokens, latency and retries spent before it failed.
    """
    if isinstance(outcome, LLMResult):
        return {
            "node": node,
            "provider": outcome.provider,
            "model": outcome.model,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "latency_ms": outcome.latency_ms,
            "retries": outcome.retries,
            "error": None,
        }
    usage = outcome.usage or LLMUsage()
    return {
        "node": node,
        "provider": outcome.provider or "unknown",
        "model": outcome.model or "unknown",
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_ms": usage.latency_ms,
        "retries": usage.retries,
        "error": f"{type(outcome).__name__}: {outcome}"[:MAX_ERROR_LENGTH],
    }
