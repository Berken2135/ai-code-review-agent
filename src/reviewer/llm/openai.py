"""OpenAI provider (Chat Completions API)."""

import time
from collections.abc import Callable
from typing import Any

import openai

from reviewer.llm.base import (
    BaseLLMClient,
    Completion,
    LLMError,
    LLMRateLimitError,
    Message,
)


class OpenAIClient(BaseLLMClient):
    provider = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        base_url: str | None = None,
        http_client: Any = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(model, api_key=api_key, max_attempts=max_attempts, sleep=sleep)
        # max_retries=0: retries are owned by BaseLLMClient so they are counted and logged once.
        # http_client lets tests inject a mock transport; the SDK's own client is used otherwise.
        self._client = openai.OpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
            base_url=base_url,
            http_client=http_client,
        )

    def _generate(self, system: str, messages: list[Message], *, json_mode: bool) -> Completion:
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *messages],
                **extra,
            )
        except openai.APIError as exc:
            raise self._to_llm_error(exc) from exc

        usage = response.usage
        return Completion(
            text=response.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    def _to_llm_error(self, exc: openai.APIError) -> LLMError:
        message = self._redact(f"OpenAI request failed: {exc.message}")
        if isinstance(exc, openai.RateLimitError):
            return LLMRateLimitError(message)
        if isinstance(exc, openai.APIStatusError):
            return LLMError(message, status_code=exc.status_code, retryable=exc.status_code >= 500)
        return LLMError(message, retryable=True)  # timeouts and connection errors
