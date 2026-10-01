"""One shared way to clean text an operator types before it is stored or shown to readers."""

from __future__ import annotations

import unicodedata

_DROPPED = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})


def one_line(text: str) -> str:
    """NFKC (full-width letters become plain ones), then every control, format (zero-width and
    bidi marks), private-use and unassigned character is replaced by a space, and runs of
    whitespace collapse to one space. A NUL byte, which the database refuses, cannot survive."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if unicodedata.category(ch) in _DROPPED else ch for ch in text)
    return " ".join(text.split())
