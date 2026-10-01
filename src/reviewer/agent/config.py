from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    """Tunable limits. Budgets are in characters (~4 chars per token) to stay provider-neutral."""

    chunk_char_budget: int = 24_000  # ~6k tokens of diff per code_review call
    max_chunks: int = 4  # beyond this the remaining files are not reviewed (and the note says so)
    max_test_files: int = 8  # existing test files sent to test_analysis
    test_file_char_budget: int = 8_000  # per existing test file
    pr_body_char_budget: int = 4_000
    min_confidence: float = 0.5  # findings below this are dropped before ranking
    max_findings: int = 15  # findings shown in the final review
    max_test_suggestions: int = 5
