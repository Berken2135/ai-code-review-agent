"""Both providers run through the same scenarios against a scripted mock HTTP transport.

The openai/anthropic SDKs are built on `httpx2`, so respx (which patches `httpx`) cannot see
their traffic. Instead each client gets an `httpx2.MockTransport`: the real SDK code still runs
(request building, response parsing, error mapping), but nothing leaves the process. The
autouse network guard in conftest.py is a second line of defence.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx2
import pytest
from pydantic import BaseModel

from reviewer.llm.anthropic import AnthropicClient
from reviewer.llm.base import BaseLLMClient, LLMError, LLMOutputError, LLMRateLimitError
from reviewer.llm.openai import OpenAIClient

API_KEY = "sk-secret-123"
GOOD = '{"summary": "looks fine", "score": 8}'


class Verdict(BaseModel):
    summary: str
    score: int


class MockAPI:
    """Scripted endpoint. `side_effect` is a list of responses/exceptions, or a callable."""

    def __init__(self) -> None:
        self.side_effect: list | Callable = []
        self.calls: list[httpx2.Request] = []
        self.http_client = httpx2.Client(transport=httpx2.MockTransport(self._handle))

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def bodies(self) -> list[str]:
        return [request.content.decode() for request in self.calls]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        if callable(self.side_effect):
            return self.side_effect(request)
        item = self.side_effect.pop(0)  # an exhausted script raises IndexError: test fails loudly
        if isinstance(item, Exception):
            raise item
        return item


def openai_ok(text: str, tokens_in: int = 11, tokens_out: int = 7) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out,
            },
        },
    )


def anthropic_ok(text: str, tokens_in: int = 11, tokens_out: int = 7) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        },
    )


@dataclass
class Kit:
    name: str
    make: Callable[..., BaseLLMClient]
    url: str
    ok: Callable[..., httpx2.Response]

    def error(self, status: int, message: str = "boom") -> httpx2.Response:
        return httpx2.Response(status, json={"error": {"message": message}})


KITS = [
    Kit(
        "openai",
        lambda api_key=API_KEY, **kw: OpenAIClient(api_key, "gpt-test", **kw),
        "https://api.openai.com/v1/chat/completions",
        openai_ok,
    ),
    Kit(
        "anthropic",
        lambda api_key=API_KEY, **kw: AnthropicClient(api_key, "claude-test", **kw),
        "https://api.anthropic.com/v1/messages",
        anthropic_ok,
    ),
]


@pytest.fixture(params=KITS, ids=lambda k: k.name)
def kit(request) -> Kit:
    return request.param


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def route() -> MockAPI:
    return MockAPI()


@pytest.fixture
def client(kit, route, sleeps) -> BaseLLMClient:
    return kit.make(http_client=route.http_client, sleep=sleeps.append, max_attempts=3)


# --- success and metrics -----------------------------------------------------------------------


def test_complete_returns_text_and_metrics(kit, client, route):
    route.side_effect = [kit.ok("hello", 11, 7)]

    result = client.complete("be brief", "say hello")

    assert result.text == "hello"
    assert result.parsed is None
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert result.provider == kit.name
    assert result.model == client.model
    assert result.retries == 0
    assert result.latency_ms >= 0
    assert not result.repaired
    assert str(route.calls[0].url) == kit.url
    assert "be brief" in route.bodies()[0]
    assert "say hello" in route.bodies()[0]


def test_structured_success_parses_and_captures_tokens(kit, client, route):
    route.side_effect = [kit.ok(GOOD, 20, 9)]

    result = client.complete_structured("review", "diff here", Verdict)

    assert result.parsed == Verdict(summary="looks fine", score=8)
    assert (result.input_tokens, result.output_tokens) == (20, 9)
    assert not result.repaired
    assert "score" in route.bodies()[0]  # the JSON schema was sent to the model


def test_structured_tolerates_code_fences(kit, client, route):
    route.side_effect = [kit.ok(f"```json\n{GOOD}\n```")]

    assert client.complete_structured("s", "u", Verdict).parsed.score == 8


def test_api_key_is_sent_as_auth_header(kit, client, route):
    route.side_effect = [kit.ok("hi")]

    client.complete("s", "u")

    headers = route.calls[0].headers
    assert API_KEY in (headers.get("authorization", "") + headers.get("x-api-key", ""))


def test_openai_uses_json_mode_only_for_structured_output(route, sleeps):
    kit = KITS[0]
    client = kit.make(http_client=route.http_client, sleep=sleeps.append)
    route.side_effect = [kit.ok("plain"), kit.ok(GOOD)]

    client.complete("s", "u")
    client.complete_structured("s", "u", Verdict)

    plain, structured = (json.loads(body) for body in route.bodies())
    assert "response_format" not in plain
    assert structured["response_format"] == {"type": "json_object"}


# --- retries -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_transient_status_then_succeeds(kit, client, route, sleeps, status):
    route.side_effect = [kit.error(status), kit.ok("recovered")]

    result = client.complete("s", "u")

    assert result.text == "recovered"
    assert result.retries == 1
    assert len(sleeps) == 1
    assert route.call_count == 2


def test_retries_timeouts(kit, client, route, sleeps):
    route.side_effect = [httpx2.ConnectTimeout("timed out"), kit.ok("recovered")]

    result = client.complete("s", "u")

    assert result.retries == 1
    assert len(sleeps) == 1


def test_rate_limit_exhaustion_raises_typed_error_with_usage(kit, client, route, sleeps):
    route.side_effect = lambda request: kit.error(429)

    with pytest.raises(LLMRateLimitError) as exc_info:
        client.complete("s", "u")

    assert route.call_count == 3
    assert len(sleeps) == 2
    assert exc_info.value.status_code == 429
    assert exc_info.value.usage.retries == 2
    assert exc_info.value.provider == kit.name


def test_server_error_exhaustion_raises_llm_error(kit, client, route):
    route.side_effect = lambda request: kit.error(500)

    with pytest.raises(LLMError) as exc_info:
        client.complete("s", "u")

    assert not isinstance(exc_info.value, LLMRateLimitError)
    assert exc_info.value.status_code == 500
    assert route.call_count == 3


def test_400_is_not_retried(kit, client, route, sleeps):
    route.side_effect = [kit.error(400, "bad request")]

    with pytest.raises(LLMError) as exc_info:
        client.complete("s", "u")

    assert exc_info.value.status_code == 400
    assert not exc_info.value.retryable
    assert route.call_count == 1
    assert sleeps == []


def test_api_key_is_redacted_from_error_messages(kit, client, route):
    route.side_effect = [kit.error(401, f"Incorrect API key provided: {API_KEY}")]

    with pytest.raises(LLMError) as exc_info:
        client.complete("s", "u")

    assert API_KEY not in str(exc_info.value)


def test_redact_helper_scrubs_key(client):
    assert API_KEY not in client._redact(f"prefix {API_KEY} suffix")


# --- structured output: repair -----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_reply",
    ["this is not json", '{"summary": "ok", "score": "not-a-number"}'],
    ids=["invalid-json", "schema-violation"],
)
def test_repair_retry_succeeds(kit, client, route, bad_reply):
    route.side_effect = [kit.ok(bad_reply, 20, 5), kit.ok(GOOD, 30, 6)]

    result = client.complete_structured("s", "u", Verdict)

    assert result.parsed == Verdict(summary="looks fine", score=8)
    assert result.repaired
    assert (result.input_tokens, result.output_tokens) == (50, 11)  # both calls are counted
    first, second = route.bodies()
    assert "not valid" not in first
    assert "not valid" in second  # validation feedback was sent back...
    assert json.dumps(bad_reply)[1:-1] in second  # ...together with the model's bad reply


def test_repair_failure_raises_output_error_after_exactly_one_repair(kit, client, route):
    route.side_effect = [kit.ok("nope", 20, 5), kit.ok("still nope", 30, 6)]

    with pytest.raises(LLMOutputError) as exc_info:
        client.complete_structured("s", "u", Verdict)

    assert route.call_count == 2  # original + exactly one repair
    assert "after one repair" in str(exc_info.value)
    assert "still nope" not in str(exc_info.value)  # the model output is not echoed
    assert exc_info.value.usage.input_tokens == 50
    assert exc_info.value.usage.output_tokens == 11


def test_transient_error_during_repair_is_retried_but_repair_stays_single(kit, client, route):
    route.side_effect = [kit.ok("nope"), kit.error(503), kit.ok(GOOD)]

    result = client.complete_structured("s", "u", Verdict)

    assert result.repaired
    assert result.retries == 1
