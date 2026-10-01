"""code_review (LLM): bugs, security, performance, quality and edge cases, per diff chunk."""

from reviewer.agent.nodes.common import NodeFn, guarded
from reviewer.agent.prompts import render, untrusted
from reviewer.agent.state import CodeReviewOutput, Finding, ReviewState
from reviewer.agent.validation import validate_findings
from reviewer.llm.base import LLMClient, LLMError
from reviewer.llm.records import llm_call_row


def pr_context(state: ReviewState) -> str:
    text = f"Title: {state.get('pr_title', '')}\n\nDescription:\n{state.get('pr_body') or '(none)'}"
    return untrusted("PR TITLE AND DESCRIPTION", text)


def make_code_review(llm: LLMClient) -> NodeFn:
    @guarded("code_review", critical=True)
    def code_review(state: ReviewState) -> dict:
        chunks = state.get("chunks", [])
        if not chunks:
            return {
                "code_findings": [],
                "chunks_failed": 0,
                "node_status": {"code_review": "skipped"},
            }

        context = pr_context(state)
        findings: list[Finding] = []
        calls: list[dict] = []
        failures: list[str] = []
        for chunk in chunks:
            system, user = render(
                "code_review",
                chunk_info=f"part {chunk.index + 1} of {len(chunks)}",
                pr_context=context,
                diff=untrusted("DIFF", chunk.text),
            )
            try:
                result = llm.complete_structured(system, user, CodeReviewOutput)
            except LLMError as exc:
                calls.append(llm_call_row("code_review", exc))
                failures.append(f"{type(exc).__name__}: {exc}")
                continue
            calls.append(llm_call_row("code_review", result))
            findings.extend(result.parsed.findings)

        if len(failures) == len(chunks):
            message = f"code_review failed on every chunk: {failures[-1]}"[:500]
            return {
                "llm_calls": calls,
                "errors": [message],
                "fatal_error": message,
                "node_status": {"code_review": "failed"},
            }

        valid, stats = validate_findings(
            findings, set(state["reviewed_files"]), state.get("line_ranges", {})
        )
        update: dict = {
            "code_findings": valid,
            "chunks_failed": len(failures),
            "llm_calls": calls,
            "validation": {"code_review": stats},
            "node_status": {"code_review": "degraded" if failures else "ok"},
        }
        if failures:  # partial coverage: ship the review, but say so
            update["errors"] = [
                f"code_review: {len(failures)} of {len(chunks)} chunks failed ({failures[-1]})"[
                    :500
                ]
            ]
        return update

    return code_review
