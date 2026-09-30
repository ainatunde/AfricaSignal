"""The real NERC fixtures, and the OCR text of the tariff PDF (read once per test run)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from africasignal.evidence.ocr import OcrResult, ocr_pdf, tesseract_available

NERC = Path(__file__).resolve().parents[2] / "fixtures" / "nerc"
TARIFF_PDF = "IE_HOLDCO_YSS_September_2026_093.pdf"
ORDER_PDF = "Amended-order-on-nonadmin-opex_04092026171143.pdf"
TARIFF_URL = "https://nerc.gov.ng/wp-content/uploads/2026/09/" + TARIFF_PDF
ORDER_URL = "https://nerc.gov.ng/wp-content/uploads/2026/09/" + ORDER_PDF

needs_tesseract = pytest.mark.skipif(not tesseract_available(), reason="tesseract is not installed")


def read(name: str) -> bytes:
    return (NERC / name).read_bytes()


@cache
def tariff_ocr() -> OcrResult:
    """OCR of the drawn Ikeja Electric schedule (12 pages, about 30 seconds): done once."""
    return ocr_pdf(read(TARIFF_PDF))
