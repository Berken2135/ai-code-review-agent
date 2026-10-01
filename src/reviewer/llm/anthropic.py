"""Anthropic provider (Messages API)."""

import time
from collections.abc import Callable
from typing import Any

import anthropic

from reviewer.llm.base import (
    BaseLLMClient,
    Completion,
    LLMError,
    LLMRateLimitError,
    Message,
)


class AnthropicClient(BaseLLMClient):
    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        base_url: str | None = None,
        http_client: Any = None,
        max_tokens: int = 4096,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(model, api_key=api_key, max_attempts=max_attempts, sleep=sleep)
        self._max_tokens = max_tokens
        # max_retries=0: retries are owned by BaseLLMClient so they are counted and logged once.
        # http_client lets tests inject a mock transport; the SDK's own client is used otherwise.
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
            base_url=base_url,
            http_client=http_client,
        )

    def _generate(self, system: str, messages: list[Message], *, json_mode: bool) -> Completion:
        # json_mode is unused: Anthropic has no JSON mode, the schema instructions in the system
        # prompt plus the repair round in BaseLLMClient cover it.
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise self._to_llm_error(exc) from exc

        return Completion(
            text="".join(block.text for block in response.content if block.type == "text"),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def _to_llm_error(self, exc: anthropic.APIError) -> LLMError:
        message = self._redact(f"Anthropic request failed: {exc.message}")
        if isinstance(exc, anthropic.RateLimitError):
            return LLMRateLimitError(message)
        if isinstance(exc, anthropic.APIStatusError):
            return LLMError(message, status_code=exc.status_code, retryable=exc.status_code >= 500)
        return LLMError(message, retryable=True)  # timeouts and connection errors
