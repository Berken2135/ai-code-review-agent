"""Scripted LLM client for tests: queued responses, recorded calls, injectable errors."""

from collections import deque
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from reviewer.llm.base import LLMOutputError, LLMResult

Response = str | dict[str, Any] | BaseModel | Exception


@dataclass(frozen=True)
class FakeCall:
    method: str  # "complete" | "complete_structured"
    system: str
    user: str
    schema: type[BaseModel] | None = None


class FakeLLM:
    """Returns queued responses in order. Queue an Exception to make that call raise it."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, responses: list[Response] | None = None) -> None:
        self._queue: deque[Response] = deque(responses or [])
        self.calls: list[FakeCall] = []
        self.input_tokens = 10
        self.output_tokens = 5

    def queue(self, *responses: Response) -> None:
        self._queue.extend(responses)

    def complete(self, system: str, user: str) -> LLMResult[BaseModel]:
        self.calls.append(FakeCall("complete", system, user))
        response = self._next()
        text = response.model_dump_json() if isinstance(response, BaseModel) else str(response)
        return self._result(text, None)

    def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T]
    ) -> LLMResult[T]:
        self.calls.append(FakeCall("complete_structured", system, user, schema))
        response = self._next()
        try:
            if isinstance(response, BaseModel):
                parsed = schema.model_validate(response.model_dump())
            elif isinstance(response, dict):
                parsed = schema.model_validate(response)
            else:
                parsed = schema.model_validate_json(response)
        except ValidationError as exc:
            raise LLMOutputError(f"fake response does not match {schema.__name__}") from exc
        return self._result(parsed.model_dump_json(), parsed)

    def _next(self) -> str | dict[str, Any] | BaseModel:
        if not self._queue:
            raise AssertionError("FakeLLM has no queued response for this call")
        response = self._queue.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    def _result[T: BaseModel](self, text: str, parsed: T | None) -> LLMResult[T]:
        return LLMResult(
            text=text,
            parsed=parsed,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_ms=0,
            retries=0,
            model=self.model,
            provider=self.provider,
        )
