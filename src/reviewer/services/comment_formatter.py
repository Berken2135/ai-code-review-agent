"""Render the PR comment from structured data. Deterministic code; the LLM never writes markup.

All LLM-written text is treated as untrusted: HTML is escaped, @mentions are neutralised (no
notification spam) and code is fenced so it cannot break out of its block. The result is kept
under GitHub's 65,536-character comment limit by dropping the lowest-priority findings first.
"""

import re
from pathlib import PurePosixPath

from reviewer.agent.state import Finding, ReviewState
from reviewer.github.client import BOT_MARKER

GITHUB_COMMENT_LIMIT = 65_536
SAFE_LIMIT = 65_000  # margin for how GitHub counts characters
RESERVED_FOR_OMISSION_NOTE = 400

MAX_DESCRIPTION = 2_500
MAX_SUGGESTION = 1_500
MAX_TEST_CODE = 8_000
MAX_NOTE = 400

VERDICT_LABELS = {
    "approve": "✅ Approve",
    "comment": "💬 Comment",
    "request_changes": "❌ Changes requested",
}
SEVERITY_HEADINGS = {"high": "🔴 High", "medium": "🟠 Medium", "low": "🟡 Low"}
FENCE_LANGUAGES = {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript"}

_TAG = re.compile(r"<(?=[A-Za-z/!])")
_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")


def escape(text: str) -> str:
    """Neutralise HTML, HTML comments and @mentions in untrusted prose."""
    text = text.replace("<!--", "&lt;!--")
    text = _TAG.sub("&lt;", text)
    return _MENTION.sub("@​", text)


def cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def code_span(text: str) -> str:
    return f"`{text.replace('`', "'")}`"


def fenced(code: str, language: str = "") -> str:
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{code.strip(chr(10))}\n{fence}"


def _location(finding: Finding) -> str:
    if not finding.file_path:
        return ""
    where = finding.file_path
    if finding.line_start is not None:
        where += f":{finding.line_start}"
        if finding.line_end and finding.line_end != finding.line_start:
            where += f"-{finding.line_end}"
    return code_span(where)


def render_finding(finding: Finding) -> str:
    meta = [m for m in (_location(finding), finding.category, f"{finding.confidence:.0%}") if m]
    lines = [
        f"**{escape(finding.title)}**  ",
        " · ".join(meta),
        "",
        escape(cap(finding.description, MAX_DESCRIPTION)),
    ]
    if finding.suggestion:
        lines += ["", f"> **Suggestion:** {escape(cap(finding.suggestion, MAX_SUGGESTION))}"]
    return "\n".join(lines)


def render_test_suggestion(finding: Finding) -> str:
    where = f" — {code_span(finding.file_path)}" if finding.file_path else ""
    lines = [
        "<details>",
        f"<summary>{escape(finding.title)}{where}</summary>",
        "",
        escape(cap(finding.description, MAX_DESCRIPTION)),
    ]
    if finding.suggestion:
        lines += ["", escape(cap(finding.suggestion, MAX_SUGGESTION))]
    if finding.suggested_test_code:
        language = FENCE_LANGUAGES.get(PurePosixPath(finding.file_path or "").suffix, "")
        lines += ["", fenced(cap(finding.suggested_test_code, MAX_TEST_CODE), language)]
    lines += ["", "</details>"]
    return "\n".join(lines)


def notes_from_state(state: ReviewState) -> list[str]:
    """Things the reader must know about how complete this review is."""
    status = state.get("node_status", {})
    notes = list(state.get("context_notes", []))
    if skipped := state.get("skipped_files"):
        notes.append(
            f"{len(skipped)} file(s) were skipped "
            "(lockfiles, generated, vendored, binary or deleted)."
        )
    if state.get("chunks_failed"):
        total = len(state.get("chunks", []))
        notes.append(
            f"{state['chunks_failed']} of {total} parts of the diff could not be reviewed."
        )
    if status.get("test_analysis") == "failed":
        notes.append("Test analysis failed, so this review has no test suggestions.")
    if status.get("final_review") == "degraded":
        notes.append("The final ranking step failed; findings are ordered automatically.")
    return notes


def format_comment(state: ReviewState, limit: int = SAFE_LIMIT) -> str:
    review = state["review"]
    notes = notes_from_state(state)

    header = [
        BOT_MARKER,
        "## 🤖 AI Code Review",
        "",
        f"**Verdict:** {VERDICT_LABELS[review.verdict]}",
        "",
        escape(cap(review.summary, 2_000)),
    ]
    for note in notes[:10]:
        header += ["", f"> ⚠️ {escape(cap(note, MAX_NOTE))}"]
    header_text = "\n".join(header)

    sha = state.get("head_sha", "")
    footer = "\n".join(
        [
            "---",
            f"<sub>Reviewed commit {code_span(sha[:7])} · "
            "Automated review, verify before acting.</sub>"
            if sha
            else "<sub>Automated review, verify before acting.</sub>",
        ]
    )

    tests = [f for f in review.findings if f.category == "missing_test"]
    sections: list[tuple[str, list[str]]] = []
    for severity, heading in SEVERITY_HEADINGS.items():
        group = [
            f for f in review.findings if f.severity == severity and f.category != "missing_test"
        ]
        if group:
            sections.append((f"### {heading} ({len(group)})", [render_finding(f) for f in group]))
    if tests:
        sections.append(
            (f"### 🧪 Suggested tests ({len(tests)})", [render_test_suggestion(f) for f in tests])
        )
    if not review.findings:
        sections.append(("### Findings", ["No issues found."]))

    parts = [header_text]
    used = len(header_text) + len(footer) + RESERVED_FOR_OMISSION_NOTE
    omitted = review.omitted_findings
    full = False
    for heading, blocks in sections:
        for i, block in enumerate(blocks):
            piece = (heading + "\n\n" if i == 0 else "") + block
            if full or used + len(piece) + 2 > limit:
                full = True
                omitted += 1
                continue
            parts.append(piece)
            used += len(piece) + 2
    if omitted:
        parts.append(f"_{omitted} more finding(s) not shown (comment size limit or findings cap)._")
    parts.append(footer)

    text = "\n\n".join(parts)
    if len(text) > GITHUB_COMMENT_LIMIT:  # unreachable with the caps above; last-resort guard
        text = text[: GITHUB_COMMENT_LIMIT - 20] + "\n\n_[truncated]_"
    return text
