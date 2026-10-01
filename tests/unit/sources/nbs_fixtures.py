"""Access to the real NBS files in ``tests/fixtures/nbs`` (see manifest.json there)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import openpyxl

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "nbs"


def manifest() -> dict[str, Any]:
    return json.loads((FIXTURES / "manifest.json").read_text())  # type: ignore[no-any-return]


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def edit(name: str, sheet: str, changes: dict[str, object]) -> bytes:
    """A copy of a real workbook with some cells changed, for tests of revisions and bad data.
    Cached values are kept as plain values."""
    wb = openpyxl.load_workbook(io.BytesIO(read(name)), data_only=True)
    ws = wb[sheet]
    for ref, value in changes.items():
        ws[ref] = value
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
