"""Console paging: bad input gives the nearest real page, never an error or an empty list."""

from __future__ import annotations

import pytest

from africasignal.operations.paging import page_of


@pytest.mark.parametrize(
    ("raw", "total", "number"),
    [(None, 45, 1), ("2", 45, 2), ("3", 45, 3), ("4", 45, 3), ("0", 45, 1), ("-2", 45, 1),
     ("abc", 45, 1), ("", 45, 1), ("5", 0, 1)],
)  # fmt: skip
def test_the_page_number_is_clamped_to_what_exists(
    raw: str | None, total: int, number: int
) -> None:
    assert page_of(raw, total, 20).number == number


def test_offsets_and_the_range_shown() -> None:
    last = page_of("3", 45, 20)
    assert (last.offset, last.first, last.last, last.pages) == (40, 41, 45, 3)
    empty = page_of(None, 0, 20)
    assert (empty.first, empty.last, empty.pages) == (0, 0, 1)
