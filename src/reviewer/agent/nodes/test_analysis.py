"""test_analysis (LLM): missing tests, with optional test code (pytest or Jest/Vitest)."""

import json

from reviewer.agent.config import AgentConfig
from reviewer.agent.files import is_testable_source
from reviewer.agent.nodes.code_review import pr_context
from reviewer.agent.nodes.common import NodeFn, guarded
from reviewer.agent.prompts import render, untrusted
from reviewer.agent.state import ReviewState, TestAnalysisOutput
from reviewer.agent.validation import validate_test_suggestions
from reviewer.llm.base import LLMClient
from reviewer.llm.records import llm_call_row


def framework_hint(state: ReviewState) -> str:
    js = state.get("js_test_framework")
    js_hint = (
        f"use {js}"
        if js
        else "framework unknown: follow the existing JS/TS tests if any, otherwise use jest"
    )
    return f"Python: use pytest. JavaScript/TypeScript: {js_hint}."


def _diff_within_budget(state: ReviewState, budget: int) -> str:
    """Chunks arrive in priority order (source first); take as many as fit one call."""
    parts: list[str] = []
    used = 0
    for chunk in state.get("chunks", []):
        if parts and used + len(chunk.text) > budget:
            break
        parts.append(chunk.text)
        used += len(chunk.text)
    return "\n\n".join(parts)


def make_test_analysis(llm: LLMClient, config: AgentConfig) -> NodeFn:
    @guarded("test_analysis", critical=False)
    def test_analysis(state: ReviewState) -> dict:
        reviewed = state.get("reviewed_files", [])
        if not state.get("chunks") or not any(is_testable_source(p) for p in reviewed):
            return {"test_findings": [], "node_status": {"test_analysis": "skipped"}}

        tests = state.get("existing_tests", {})
        existing = "\n\n".join(f"### Test file: {p}\n{c}" for p, c in tests.items())
        findings = [
            f.model_dump(include={"category", "severity", "file_path", "line_start", "title"})
            for f in state.get("code_findings", [])
        ]
        system, user = render(
            "test_analysis",
            framework_hint=framework_hint(state),
            pr_context=pr_context(state),
            diff=untrusted("DIFF", _diff_within_budget(state, config.chunk_char_budget)),
            existing_tests=untrusted(
                "EXISTING TEST FILES", existing or "(no existing tests were found for these files)"
            ),
            findings=untrusted("AUTOMATED FINDINGS", json.dumps(findings, indent=1)),
        )
        result = llm.complete_structured(system, user, TestAnalysisOutput)

        suggestions, stats = validate_test_suggestions(result.parsed.suggestions, set(reviewed))
        suggestions = [s for s in suggestions if s.confidence >= config.min_confidence]
        return {
            "test_findings": suggestions[: config.max_test_suggestions],
            "llm_calls": [llm_call_row("test_analysis", result)],
            "validation": {"test_analysis": stats},
            "node_status": {"test_analysis": "ok"},
        }

    return test_analysis
