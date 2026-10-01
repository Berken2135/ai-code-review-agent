"""Patch parsing, line-number annotation and chunking. Pure functions, no I/O."""

import re
from dataclasses import dataclass

from reviewer.schemas import ChangedFile

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
TRUNCATION_MARKER = "[... diff for this file truncated: {n} more lines not shown ...]"


def parse_hunks(patch: str) -> list[tuple[int, int]]:
    """New-file line ranges (inclusive) that each hunk covers. Pure-deletion hunks are skipped."""
    ranges = []
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match:
            start, count = int(match.group(1)), int(match.group(2) or 1)
            if count > 0:
                ranges.append((start, start + count - 1))
    return ranges


def annotate_patch(patch: str) -> str:
    """Prefix each added/context line with its new-file line number.

    Models cannot reliably derive line numbers from hunk headers, so we hand them out.
    Removed lines get a blank number column.
    """
    out: list[str] = []
    new_line = 0
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match:
            new_line = int(match.group(1))
            out.append(line)
        elif line.startswith("-") or line.startswith("\\"):
            out.append(f"{'':>5} {line}")
        else:  # '+' or context
            out.append(f"{new_line:>5} {line}")
            new_line += 1
    return "\n".join(out)


def file_section(file: ChangedFile) -> str:
    header = f"### File: {file.filename} ({file.status}, +{file.additions} -{file.deletions})"
    return f"{header}\n{annotate_patch(file.patch or '')}"


@dataclass
class ChunkResult:
    chunks: list[tuple[list[str], str]]  # (file paths, text)
    truncated_files: list[str]  # cut to fit the budget
    omitted_files: list[str]  # dropped: max_chunks reached


def truncate_section(text: str, budget: int) -> str:
    """Cut a section at a line boundary so that it (plus the marker) fits the budget."""
    marker_room = len(TRUNCATION_MARKER.format(n=99_999)) + 1
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget - marker_room:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:  # a single enormous line: hard cut
        kept = [text[: max(budget - marker_room, 0)]]
    dropped = len(lines) - len(kept)
    return "\n".join([*kept, TRUNCATION_MARKER.format(n=dropped)])


def pack_chunks(sections: list[tuple[str, str]], budget: int, max_chunks: int) -> ChunkResult:
    """Greedily pack (path, text) sections, already in priority order, into budget-sized chunks."""
    chunks: list[tuple[list[str], str]] = []
    truncated: list[str] = []
    omitted: list[str] = []
    paths: list[str] = []
    parts: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal paths, parts, size
        if parts:
            chunks.append((paths, "\n\n".join(parts)))
        paths, parts, size = [], [], 0

    for path, text in sections:
        if len(text) > budget:
            text = truncate_section(text, budget)
            truncated.append(path)
        if parts and size + len(text) + 2 > budget:
            flush()
        if not parts and len(chunks) >= max_chunks:
            omitted.append(path)
            continue
        paths.append(path)
        parts.append(text)
        size += len(text) + 2
    flush()
    return ChunkResult(chunks, truncated, omitted)
