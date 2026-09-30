import pytest

from africasignal.evidence import ocr
from africasignal.evidence.ocr import OcrUnavailable, has_text_layer, ocr_pdf
from tests.unit.evidence.test_text import make_pdf
from tests.unit.sources.nerc_fixtures import (
    ORDER_PDF,
    TARIFF_PDF,
    needs_tesseract,
    read,
    tariff_ocr,
)


def test_a_typed_order_has_a_text_layer_and_the_drawn_schedule_does_not() -> None:
    assert has_text_layer(read(ORDER_PDF))
    assert not has_text_layer(read(TARIFF_PDF))


def test_a_short_typed_pdf_with_enough_characters_per_page_counts_as_text() -> None:
    body = "The Commission approved the following tariff for Band A customers. " * 3
    assert has_text_layer(make_pdf([body]))


def test_a_missing_tesseract_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    with pytest.raises(OcrUnavailable):
        ocr_pdf(read(TARIFF_PDF), max_pages=1)


@needs_tesseract
def test_the_drawn_tariff_schedule_is_read_with_its_table_rows_on_single_lines() -> None:
    result = tariff_ocr()
    assert result.pages == 12
    assert "September 2026 Supplementary" in result.text
    assert "A - Non-MD 225.00 206.80 209.50" in result.text.splitlines()


@needs_tesseract
def test_lines_with_a_number_read_with_doubt_are_reported_but_the_clean_row_is_not() -> None:
    result = tariff_ocr()
    assert "A - Non-MD 225.00 206.80 209.50" not in result.doubtful_lines


@needs_tesseract
def test_max_pages_limits_how_much_is_read() -> None:
    result = ocr_pdf(read(TARIFF_PDF), max_pages=1)
    assert result.pages == 1
    assert "IKEJA ELECTRICITY" in result.text
