"""Load versioned prompt files from ``llm/prompts`` (for example ``claim_extract_v1.md``).

A prompt's version is its file name. Change a prompt by adding a new file with the next version
number, never by editing a released one: the version is part of the response cache key and is
stored with every claim.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


@lru_cache
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def render(template: str, **values: str) -> str:
    """Replace ``{{NAME}}`` placeholders. A placeholder without a value is an error."""
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    if "{{" in template:
        start = template.index("{{")
        raise ValueError(f"unfilled placeholder in prompt: {template[start : start + 40]!r}")
    return template
