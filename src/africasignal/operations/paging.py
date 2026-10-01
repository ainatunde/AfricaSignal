"""Paging for console lists that can grow without limit (the first NBS import alone publishes
about 150 situations, each with a draft per channel and a row per version)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    number: int  # 1-based, already clamped into range
    size: int
    total: int

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def first(self) -> int:
        """1-based position of the first row shown (0 when the list is empty)."""
        return self.offset + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.offset + self.size, self.total)


def page_of(raw: str | None, total: int, size: int) -> Page:
    """The requested page, clamped to what exists: a bad or out-of-range number gives the nearest
    real page instead of an error or an empty list."""
    try:
        number = int(raw) if raw is not None else 1
    except ValueError:
        number = 1
    pages = max(1, -(-total // size))
    return Page(min(max(number, 1), pages), size, total)
