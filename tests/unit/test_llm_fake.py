import pytest
from pydantic import BaseModel

from reviewer.llm.base import LLMError, LLMOutputError, LLMRateLimitError
from reviewer.llm.fake import FakeLLM


class Verdict(BaseModel):
    summary: str
    score: int


def test_returns_queued_responses_in_order():
    llm = FakeLLM(["first", "second"])

    assert llm.complete("s", "u1").text == "first"
    assert llm.complete("s", "u2").text == "second"


def test_records_calls():
    llm = FakeLLM(["text", Verdict(summary="ok", score=1)])

    llm.complete("sys-a", "user-a")
    llm.complete_structured("sys-b", "user-b", Verdict)

    assert [(c.method, c.system, c.user, c.schema) for c in llm.calls] == [
        ("complete", "sys-a", "user-a", None),
        ("complete_structured", "sys-b", "user-b", Verdict),
    ]


def test_queue_can_grow_after_construction():
    llm = FakeLLM()
    llm.queue("a", "b")

    assert [llm.complete("s", "u").text for _ in range(2)] == ["a", "b"]


@pytest.mark.parametrize(
    "response",
    [
        Verdict(summary="ok", score=3),
        {"summary": "ok", "score": 3},
        '{"summary": "ok", "score": 3}',
    ],
    ids=["model", "dict", "json-string"],
)
def test_structured_accepts_model_dict_or_json(response):
    result = FakeLLM([response]).complete_structured("s", "u", Verdict)

    assert result.parsed == Verdict(summary="ok", score=3)
    assert result.provider == "fake"
    assert (result.input_tokens, result.output_tokens) == (10, 5)


def test_structured_with_invalid_response_raises_output_error():
    with pytest.raises(LLMOutputError):
        FakeLLM(['{"summary": 1}']).complete_structured("s", "u", Verdict)


def test_queued_exception_is_raised_then_queue_continues():
    llm = FakeLLM([LLMRateLimitError("slow down"), "ok"])

    with pytest.raises(LLMRateLimitError):
        llm.complete("s", "u")
    assert llm.complete("s", "u").text == "ok"
    assert len(llm.calls) == 2  # the failed call was still recorded


def test_any_exception_type_can_be_queued():
    with pytest.raises(LLMError, match="boom"):
        FakeLLM([LLMError("boom")]).complete_structured("s", "u", Verdict)


def test_empty_queue_fails_loudly():
    with pytest.raises(AssertionError, match="no queued response"):
        FakeLLM().complete("s", "u")
