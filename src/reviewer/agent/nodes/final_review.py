"""final_review (LLM + deterministic guards): dedupe, rank, filter, cap, summary and verdict.

The LLM only *suggests* an order, ids to drop, a summary and a verdict. Code enforces the limits
that matter, so a confused or manipulated model cannot change the outcome:
- low-confidence findings never reach the model;
- high-severity findings can never be dropped by the model, and severity always outranks the
  model's ordering;
- unknown ids are ignored;
- the verdict cannot contradict the remaining findings (see `clamp_verdict`).
"""

import json
import re

from reviewer.agent.config import AgentConfig
from reviewer.agent.nodes.common import NodeFn, guarded
from reviewer.agent.prompts import render, untrusted
from reviewer.agent.state import (
    SEVERITY_RANK,
    FinalReviewDecision,
    Finding,
    Review,
    ReviewState,
    Verdict,
)
from reviewer.llm.base import LLMClient, LLMError
from reviewer.llm.records import llm_call_row

MAX_SUMMARY_LENGTH = 1500
DESCRIPTION_CHARS_FOR_LLM = 400


def _norm(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


def _better(a: Finding, b: Finding) -> Finding:
    return min(a, b, key=lambda f: (SEVERITY_RANK[f.severity], -f.confidence))


def _is_duplicate(a: Finding, b: Finding) -> bool:
    if a.category != b.category or a.file_path != b.file_path:
        return False
    if _norm(a.title) == _norm(b.title):
        return True
    if a.line_start is None or b.line_start is None:
        return False
    return a.line_start <= (b.line_end or b.line_start) and b.line_start <= (
        a.line_end or a.line_start
    )


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Merge findings that describe the same problem (same file and category, same title or
    overlapping lines), keeping the more severe / more confident one."""
    kept: list[Finding] = []
    for finding in findings:
        for i, existing in enumerate(kept):
            if _is_duplicate(existing, finding):
                kept[i] = _better(existing, finding)
                break
        else:
            kept.append(finding)
    return kept


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_RANK[f.severity],
            -f.confidence,
            f.file_path or "",
            f.line_start or 0,
            f.title,
        ),
    )


def clamp_verdict(verdict: Verdict, findings: list[Finding]) -> Verdict:
    """The verdict may not contradict the findings that remain."""
    severities = {f.severity for f in findings}
    if not findings:
        return "approve"
    if "high" in severities:
        return "request_changes"
    if "medium" in severities:
        return "comment" if verdict == "approve" else verdict
    return "comment"  # only low-severity findings


def _apply_decision(numbered: dict[int, Finding], decision: FinalReviewDecision) -> list[Finding]:
    """Ids the model may drop or reorder; anything it fails to mention is kept."""
    drop = {i for i in decision.drop_ids if i in numbered and numbered[i].severity != "high"}
    order: list[int] = []
    for i in decision.ranked_ids:
        if i in numbered and i not in drop and i not in order:
            order.append(i)
    order += [i for i in numbered if i not in drop and i not in order]
    position = {i: n for n, i in enumerate(order)}
    ordered = sorted(order, key=lambda i: (SEVERITY_RANK[numbered[i].severity], position[i]))
    return [numbered[i] for i in ordered]


def _counts_summary(findings: list[Finding]) -> str:
    counts = {s: sum(f.severity == s for f in findings) for s in SEVERITY_RANK}
    return (
        f"{len(findings)} issue(s): "
        f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low"
    )


def make_final_review(llm: LLMClient, config: AgentConfig) -> NodeFn:
    @guarded("final_review", critical=True)
    def final_review(state: ReviewState) -> dict:
        reviewed = state.get("reviewed_files", [])
        if not reviewed:
            skipped = len(state.get("skipped_files", {}))
            review = Review(
                verdict="approve",
                summary=f"No reviewable changes: all {skipped} changed file(s) were skipped "
                "(lockfiles, generated, vendored, binary or deleted files).",
            )
            return {"review": review, "node_status": {"final_review": "ok"}}

        candidates = list(state.get("code_findings", []))
        candidates += [s.to_finding() for s in state.get("test_findings", [])]
        confident = [f for f in candidates if f.confidence >= config.min_confidence]
        unique = sort_findings(dedupe_findings(confident))
        stats = {
            "dropped_low_confidence": len(candidates) - len(confident),
            "dropped_duplicates": len(confident) - len(unique),
        }

        if not unique:
            review = Review(
                verdict="approve",
                summary=f"No issues found in the {len(reviewed)} reviewed file(s).",
            )
            return {
                "review": review,
                "validation": {"final_review": stats},
                "node_status": {"final_review": "ok"},
            }

        numbered = {i: f for i, f in enumerate(unique, start=1)}
        payload = [
            {
                "id": i,
                "category": f.category,
                "severity": f.severity,
                "file": f.file_path,
                "lines": [f.line_start, f.line_end],
                "title": f.title,
                "description": f.description[:DESCRIPTION_CHARS_FOR_LLM],
                "confidence": round(f.confidence, 2),
            }
            for i, f in numbered.items()
        ]
        stats_line = (
            f"{len(reviewed)} files reviewed, {len(state.get('skipped_files', {}))} skipped, "
            f"diff truncated: {'yes' if state.get('diff_truncated') else 'no'}, "
            f"{len(unique)} findings"
        )
        system, user = render(
            "final_review",
            stats=stats_line,
            findings=untrusted("FINDINGS", json.dumps(payload, indent=1)),
        )

        try:
            result = llm.complete_structured(system, user, FinalReviewDecision)
        except LLMError as exc:  # degrade: deterministic ranking and summary, no LLM opinion
            findings = unique
            review = Review(
                verdict=clamp_verdict("comment", findings),
                summary=f"Found {_counts_summary(findings)}. (Automatic summary: the final "
                "ranking step was unavailable.)",
                findings=findings[: config.max_findings],
                omitted_findings=max(len(findings) - config.max_findings, 0),
            )
            return {
                "review": review,
                "llm_calls": [llm_call_row("final_review", exc)],
                "errors": [f"final_review: LLM step failed, used automatic ranking ({exc})"[:500]],
                "validation": {"final_review": stats},
                "node_status": {"final_review": "degraded"},
            }

        decision = result.parsed
        findings = _apply_decision(numbered, decision)
        stats["dropped_by_model"] = len(numbered) - len(findings)
        summary = (
            decision.summary.strip()[:MAX_SUMMARY_LENGTH] or f"Found {_counts_summary(findings)}."
        )
        review = Review(
            verdict=clamp_verdict(decision.verdict, findings),
            summary=summary,
            findings=findings[: config.max_findings],
            omitted_findings=max(len(findings) - config.max_findings, 0),
        )
        return {
            "review": review,
            "llm_calls": [llm_call_row("final_review", result)],
            "validation": {"final_review": stats},
            "node_status": {"final_review": "ok"},
        }

    return final_review
