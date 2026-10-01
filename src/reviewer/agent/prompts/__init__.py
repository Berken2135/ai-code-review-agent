"""Prompt loading. Templates are Markdown files: system prompt, a `---USER---` line, user template.

Trust model: the system prompt is ours. Anything that originates from the PR (title, description,
diff, file contents) or from an earlier LLM step that read them is untrusted DATA and is only ever
placed in the user message, inside `untrusted()` delimiters carrying a random per-call id, so
content cannot forge a closing marker.
"""

import secrets
from pathlib import Path
from string import Template

_DIR = Path(__file__).parent
USER_MARKER = "---USER---"


def untrusted(label: str, text: str) -> str:
    """Wrap untrusted text in delimiters with an unguessable id."""
    nonce = secrets.token_hex(6)
    return f"<<<BEGIN UNTRUSTED {label} [{nonce}]>>>\n{text}\n<<<END UNTRUSTED {label} [{nonce}]>>>"


def render(name: str, **data: str) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for a template. Values are inserted verbatim, once."""
    system, user = (_DIR / f"{name}.md").read_text(encoding="utf-8").split(USER_MARKER, 1)
    # Template substitution is single-pass: `$name` inside a value is never expanded again.
    return system.strip(), Template(user).substitute(**data).strip()
