import functools
import time
from collections.abc import Callable
from typing import Any

import structlog

from reviewer.agent.state import ReviewState
from reviewer.llm.base import LLMError
from reviewer.llm.records import llm_call_row

log = structlog.get_logger(__name__)

NodeFn = Callable[[ReviewState], dict[str, Any]]
MAX_ERROR_LENGTH = 500


def error_message(node: str, exc: BaseException) -> str:
    return f"{node} failed: {type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


def guarded(name: str, *, critical: bool) -> Callable[[NodeFn], NodeFn]:
    """Turn an unexpected exception into recorded state instead of crashing the graph.

    A critical node's failure sets `fatal_error`, which ends the graph. Any LLM call that failed
    is still recorded as an llm_calls row (with the tokens and retries it used).
    """

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        def wrapper(state: ReviewState) -> dict[str, Any]:
            start = time.perf_counter()
            try:
                update = fn(state)
            except Exception as exc:
                message = error_message(name, exc)
                update = {"errors": [message], "node_status": {name: "failed"}}
                if isinstance(exc, LLMError):
                    update["llm_calls"] = [llm_call_row(name, exc)]
                if critical:
                    update["fatal_error"] = message
            log.info(
                "agent_node_finished",
                node=name,
                status=update.get("node_status", {}).get(name),
                duration_ms=round((time.perf_counter() - start) * 1000),
                fatal=bool(update.get("fatal_error")),
            )
            return update

        return wrapper

    return decorator
