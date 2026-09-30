"""Text normalisation shared by the loader and the resolver."""

from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
# Words that say what kind of place a name is, not which one. Removed only from the end/start of
# the text, and only in ``strip_qualifiers``.
_TRAILING_QUALIFIERS = (
    " local government area",
    " local government",
    " lga",
    " state",
    " city",
)
_LEADING_QUALIFIERS = ("the ",)


def normalise(text: str) -> str:
    """Lowercase, strip accents and punctuation, and collapse whitespace.

    Hyphens and slashes become spaces ("Eti-Osa" and "Eti Osa" match); apostrophes are removed
    ("Jama'are" becomes "jamaare").
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("'", "").replace("’", "")
    text = text.replace("-", " ").replace("/", " ")
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def strip_qualifiers(norm: str) -> str:
    """Drop "state", "lga", "city", "local government area" from the end and "the" from the start
    of an already normalised text ("lagos state" becomes "lagos")."""
    changed = True
    while changed:
        changed = False
        for suffix in _TRAILING_QUALIFIERS:
            if norm.endswith(suffix):
                norm = norm[: -len(suffix)].strip()
                changed = True
        for prefix in _LEADING_QUALIFIERS:
            if norm.startswith(prefix):
                norm = norm[len(prefix) :].strip()
                changed = True
    return norm


def slugify(text: str) -> str:
    return normalise(text).replace(" ", "-")
