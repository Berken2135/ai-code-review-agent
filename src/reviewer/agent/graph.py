"""LangGraph wiring: fetch_context -> code_review -> test_analysis -> final_review.

Failure policy:
- fetch_context or code_review fails -> the graph stops and `run_review` raises ReviewFailedError
  (carrying the final state, so LLM calls made so far can still be recorded);
- test_analysis fails -> the review still ships without test findings (the comment says so);
- final_review's LLM step fails -> the review ships with automatic ranking (status "degraded").
"""

import structlog
from langgraph.graph import END, START, StateGraph

from reviewer.agent.config import AgentConfig
from reviewer.agent.nodes.code_review import make_code_review
from reviewer.agent.nodes.fetch_context import make_fetch_context
from reviewer.agent.nodes.final_review import make_final_review
from reviewer.agent.nodes.test_analysis import make_test_analysis
from reviewer.agent.ports import GitHubPort
from reviewer.agent.state import ReviewState
from reviewer.llm.base import LLMClient

log = structlog.get_logger(__name__)


class ReviewFailedError(Exception):
    """A critical node failed. `state` holds everything collected up to that point."""

    def __init__(self, message: str, state: ReviewState) -> None:
        super().__init__(message)
        self.state = state


def _stop_on_fatal(next_node: str):
    def route(state: ReviewState) -> str:
        return END if state.get("fatal_error") else next_node

    return route


def build_graph(github: GitHubPort, llm: LLMClient, config: AgentConfig | None = None):
    config = config or AgentConfig()
    graph = StateGraph(ReviewState)
    graph.add_node("fetch_context", make_fetch_context(github, config))
    graph.add_node("code_review", make_code_review(llm))
    graph.add_node("test_analysis", make_test_analysis(llm, config))
    graph.add_node("final_review", make_final_review(llm, config))

    graph.add_edge(START, "fetch_context")
    graph.add_conditional_edges(
        "fetch_context",
        _stop_on_fatal("code_review"),
        {"code_review": "code_review", END: END},
    )
    graph.add_conditional_edges(
        "code_review",
        _stop_on_fatal("test_analysis"),
        {"test_analysis": "test_analysis", END: END},
    )
    graph.add_edge("test_analysis", "final_review")
    graph.add_edge("final_review", END)
    return graph.compile()


def run_review(
    github: GitHubPort,
    llm: LLMClient,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    config: AgentConfig | None = None,
) -> ReviewState:
    """Run the pipeline. Returns the final state; raises ReviewFailedError on a critical failure."""
    initial: ReviewState = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "errors": [],
        "llm_calls": [],
        "node_status": {},
        "validation": {},
    }
    state: ReviewState = build_graph(github, llm, config).invoke(initial)
    log.info(
        "agent_review_finished",
        repo=repo,
        pr=pr_number,
        node_status=state.get("node_status"),
        llm_calls=len(state.get("llm_calls", [])),
        failed=bool(state.get("fatal_error")),
    )
    if state.get("fatal_error"):
        raise ReviewFailedError(state["fatal_error"], state)
    return state
