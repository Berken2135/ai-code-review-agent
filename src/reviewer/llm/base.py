"""Provider-agnostic LLM contract plus the shared retry / structured-output / metrics logic.

Providers only implement `_generate` (one raw API call that raises typed `LLMError`s).
Everything else lives here so both providers behave identically:

- transient failures (429, 5xx, timeouts, connection errors) are retried with exponential
  backoff and jitter; other 4xx errors are not;
- structured output is validated against a pydantic schema, with exactly one repair round
  that sends the validation error back to the model;
- token usage, latency and retry counts are captured for observability.

Prompts and completions are never logged; only metadata is.
"""

import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ValidationError
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

log = structlog.get_logger(__name__)

Message = dict[str, str]


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    retries: int = 0


class LLMError(Exception):
    """Base class for all LLM failures. `usage`/`provider`/`model` are filled in by the client."""

    def __init__(
        self, message: str, *, status_code: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.usage: LLMUsage | None = None
        self.provider: str | None = None
        self.model: str | None = None


class LLMRateLimitError(LLMError):
    def __init__(self, message: str, *, status_code: int = 429) -> None:
        super().__init__(message, status_code=status_code, retryable=True)


class LLMOutputError(LLMError):
    """The model's output could not be turned into the requested schema (after one repair)."""


class LLMConfigError(LLMError):
    """The LLM layer is misconfigured (unknown provider, missing API key)."""


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    text: str
    parsed: T | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    retries: int
    model: str
    provider: str
    repaired: bool = False


@dataclass(frozen=True)
class Completion:
    """One raw provider response."""

    text: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str) -> LLMResult[BaseModel]:
        """Free-text completion. `parsed` is None."""
        ...

    def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T]
    ) -> LLMResult[T]:
        """Completion parsed into `schema`. Raises LLMOutputError if it cannot be."""
        ...


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE.match(stripped)
    return match.group(1) if match else stripped


def _summarize(exc: ValidationError) -> str:
    """Compact validation problems. Deliberately excludes the offending input."""
    problems = exc.errors(include_input=False, include_url=False, include_context=False)
    joined = "; ".join(f"{'.'.join(map(str, p['loc'])) or 'output'}: {p['msg']}" for p in problems)
    return joined[:1500]


def _json_instructions(schema: type[BaseModel]) -> str:
    return (
        "Respond with a single JSON object that conforms to this JSON schema. "
        "Output only the JSON: no prose, no code fences.\n" + json.dumps(schema.model_json_schema())
    )


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, LLMError) and exc.retryable


def _elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


class BaseLLMClient(ABC):
    provider: str

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.model = model
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._sleep = sleep

    @abstractmethod
    def _generate(self, system: str, messages: list[Message], *, json_mode: bool) -> Completion:
        """One API call. Must raise LLMError (with `retryable` set) instead of SDK exceptions."""

    def _redact(self, text: str) -> str:
        """Scrub the API key from text before it goes into an exception message."""
        return text.replace(self._api_key, "***")[:500] if self._api_key else text[:500]

    # --- public API -------------------------------------------------------------------------

    def complete(self, system: str, user: str) -> LLMResult[BaseModel]:
        def operation(usage: LLMUsage) -> tuple[str, None, bool]:
            completion = self._call(system, [{"role": "user", "content": user}], False, usage)
            return completion.text, None, False

        return self._timed(operation)

    def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T]
    ) -> LLMResult[T]:
        full_system = f"{system}\n\n{_json_instructions(schema)}"
        messages: list[Message] = [{"role": "user", "content": user}]

        def operation(usage: LLMUsage) -> tuple[str, T, bool]:
            completion = self._call(full_system, messages, True, usage)
            try:
                return completion.text, self._parse(completion.text, schema), False
            except ValidationError as first_error:
                feedback = _summarize(first_error)

            # Exactly one repair round: show the model its reply and what was wrong with it.
            messages.extend(
                [
                    {"role": "assistant", "content": completion.text},
                    {
                        "role": "user",
                        "content": (
                            f"Your previous reply was not valid. Problems: {feedback}\n"
                            "Reply again with only the corrected JSON object."
                        ),
                    },
                ]
            )
            completion = self._call(full_system, messages, True, usage)
            try:
                return completion.text, self._parse(completion.text, schema), True
            except ValidationError as second_error:
                raise LLMOutputError(
                    f"model output failed validation after one repair: {_summarize(second_error)}"
                ) from second_error

        return self._timed(operation)

    # --- internals --------------------------------------------------------------------------

    @staticmethod
    def _parse[T: BaseModel](text: str, schema: type[T]) -> T:
        return schema.model_validate_json(_strip_fences(text))

    def _call(
        self, system: str, messages: list[Message], json_mode: bool, usage: LLMUsage
    ) -> Completion:
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_random_exponential(multiplier=1, max=30),
            retry=retry_if_exception(_is_retryable),
            before_sleep=self._log_retry,
            sleep=self._sleep,
            reraise=True,
        )
        try:
            completion = retrying(self._generate, system, messages, json_mode=json_mode)
        finally:
            usage.retries += retrying.statistics.get("attempt_number", 1) - 1
        usage.input_tokens += completion.input_tokens
        usage.output_tokens += completion.output_tokens
        return completion

    def _timed[T: BaseModel](
        self, operation: Callable[[LLMUsage], tuple[str, T | None, bool]]
    ) -> LLMResult[T]:
        usage = LLMUsage()
        start = time.perf_counter()
        try:
            text, parsed, repaired = operation(usage)
        except LLMError as exc:
            usage.latency_ms = _elapsed_ms(start)
            exc.usage, exc.provider, exc.model = usage, self.provider, self.model
            log.warning(
                "llm_call_failed",
                provider=self.provider,
                model=self.model,
                error=type(exc).__name__,
                status_code=exc.status_code,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=usage.latency_ms,
                retries=usage.retries,
            )
            raise
        usage.latency_ms = _elapsed_ms(start)
        log.info(
            "llm_call",
            provider=self.provider,
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=usage.latency_ms,
            retries=usage.retries,
            repaired=repaired,
        )
        return LLMResult(
            text=text,
            parsed=parsed,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=usage.latency_ms,
            retries=usage.retries,
            model=self.model,
            provider=self.provider,
            repaired=repaired,
        )

    def _log_retry(self, state: RetryCallState) -> None:
        exc: Any = state.outcome.exception() if state.outcome else None
        log.warning(
            "llm_retry",
            provider=self.provider,
            model=self.model,
            attempt=state.attempt_number,
            error=type(exc).__name__,
            status_code=getattr(exc, "status_code", None),
        )
