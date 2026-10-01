"""64-bit SimHash for near-duplicate detection, stored as a signed bigint."""

from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"\w+", re.UNICODE)
_BITS = 64


def _shingles(text: str, size: int = 3) -> list[str]:
    tokens = _TOKEN.findall(text.lower())
    if len(tokens) < size:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def simhash(text: str) -> int | None:
    """SimHash of ``text`` over word 3-grams, as a signed 64-bit integer. None for empty text."""
    features = _shingles(text)
    if not features:
        return None
    counts = [0] * _BITS
    for feature in features:
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        for bit in range(_BITS):
            counts[bit] += 1 if (h >> bit) & 1 else -1
    value = sum(1 << bit for bit in range(_BITS) if counts[bit] > 0)
    return value - (1 << _BITS) if value >= 1 << (_BITS - 1) else value


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two signed 64-bit simhashes."""
    return ((a ^ b) & ((1 << _BITS) - 1)).bit_count()
