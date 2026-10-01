"""Deterministic clean-up of LLM output. LLMs hallucinate file paths and line numbers, so this is
code, not a prompt: unknown files are fixed or dropped, and line numbers are clamped to the diff.
"""

from pathlib import PurePosixPath

from reviewer.agent.state import Finding, TestSuggestion

LineRanges = dict[str, list[tuple[int, int]]]


def resolve_path(raw: str, files: set[str]) -> str | None:
    """Map an LLM-written path onto a real changed file, or None if it cannot be trusted."""
    path = raw.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path in files:
        return path
    for prefix in ("a/", "b/", "/"):  # diff-style or absolute prefixes
        if path.startswith(prefix) and path[len(prefix) :] in files:
            return path[len(prefix) :]
    # A bare or partially qualified name is accepted only when it is unambiguous.
    matches = [f for f in files if f == path or f.endswith("/" + path)]
    if not matches:
        matches = [f for f in files if PurePosixPath(f).name == PurePosixPath(path).name]
    return matches[0] if len(matches) == 1 else None


def clamp_lines(
    start: int | None, end: int | None, ranges: list[tuple[int, int]]
) -> tuple[int | None, int | None]:
    """Snap a line range onto lines that actually appear in the diff hunks."""
    if start is None and end is None:
        return None, None
    if not ranges:
        return None, None
    start = end if start is None else start
    end = start if end is None else end
    if end < start:
        start, end = end, start

    def distance(r: tuple[int, int]) -> int:
        return 0 if r[0] <= start <= r[1] else min(abs(start - r[0]), abs(start - r[1]))

    hunk = min(ranges, key=distance)
    new_start = min(max(start, hunk[0]), hunk[1])
    new_end = min(max(end, new_start), hunk[1])
    return new_start, new_end


def validate_findings(
    findings: list[Finding], files: set[str], line_ranges: LineRanges
) -> tuple[list[Finding], dict[str, int]]:
    stats = {"dropped_unknown_file": 0, "fixed_path": 0, "clamped_lines": 0}
    valid: list[Finding] = []
    for finding in findings:
        if finding.file_path is None:  # a general finding: keep it, but it has no line numbers
            valid.append(finding.model_copy(update={"line_start": None, "line_end": None}))
            continue
        path = resolve_path(finding.file_path, files)
        if path is None:
            stats["dropped_unknown_file"] += 1
            continue
        if path != finding.file_path:
            stats["fixed_path"] += 1
        start, end = clamp_lines(finding.line_start, finding.line_end, line_ranges.get(path, []))
        if (start, end) != (finding.line_start, finding.line_end):
            stats["clamped_lines"] += 1
        valid.append(
            finding.model_copy(update={"file_path": path, "line_start": start, "line_end": end})
        )
    return valid, stats


def safe_test_path(raw: str | None) -> str | None:
    """A proposed test file path must be relative and stay inside the repo."""
    if not raw:
        return None
    path = raw.strip().replace("\\", "/")
    if path.startswith("/") or ".." in PurePosixPath(path).parts or ":" in path.split("/")[0]:
        return None
    return path


def validate_test_suggestions(
    suggestions: list[TestSuggestion], files: set[str]
) -> tuple[list[TestSuggestion], dict[str, int]]:
    stats = {"dropped_unknown_file": 0, "fixed_path": 0, "dropped_unsafe_test_path": 0}
    valid: list[TestSuggestion] = []
    for suggestion in suggestions:
        path = suggestion.file_path
        if path is not None:
            resolved = resolve_path(path, files)
            if resolved is None:
                stats["dropped_unknown_file"] += 1
                continue
            stats["fixed_path"] += resolved != path
            path = resolved
        test_file = safe_test_path(suggestion.test_file)
        if suggestion.test_file and test_file is None:
            stats["dropped_unsafe_test_path"] += 1
        valid.append(suggestion.model_copy(update={"file_path": path, "test_file": test_file}))
    return valid, stats
