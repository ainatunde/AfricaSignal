"""Small unit tests for findings of the AS-042 security review."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from africasignal.evidence.ocr import MAX_PAGE_PIXELS, RESOLUTION_DPI, render_dpi
from africasignal.sources import nbs, nbs_workbook
from africasignal.sources.nbs_workbook import NbsParseError, check_zip_size
from africasignal.web.queries import web_url
from africasignal.web.user_dep import safe_next

ROOT = Path(__file__).resolve().parents[2]


# --- redirect targets ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["/\t/evil.example", "/\n/evil.example", "/ok\r\nSet-Cookie: x=1", "/a\x00b", "//evil.example"],
)
def test_safe_next_refuses_control_characters_and_protocol_relative_paths(value: str) -> None:
    assert safe_next(value) is None


def test_safe_next_keeps_an_ordinary_local_path() -> None:
    assert safe_next("/s/price-pms?ref=x") == "/s/price-pms?ref=x"


# --- evidence links -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://nerc.gov.ng/order.pdf", "https://nerc.gov.ng/order.pdf"),
        ("HTTP://example.ng/a", "HTTP://example.ng/a"),
        ("javascript:alert(1)", ""),
        ("JaVaScRiPt:alert(1)", ""),
        ("data:text/html,<script>alert(1)</script>", ""),
        ("//evil.example/x", ""),
        ("", ""),
    ],
)
def test_web_url_only_passes_http_and_https(url: str, expected: str) -> None:
    assert web_url(url) == expected


# --- OCR page size ------------------------------------------------------------------------------


def test_a_normal_page_is_rendered_at_full_resolution() -> None:
    assert render_dpi(595, 842) == RESOLUTION_DPI  # A4 in points


def test_an_enormous_declared_page_is_rendered_at_a_lower_resolution() -> None:
    dpi = render_dpi(14_400, 14_400)  # the largest page a PDF viewer accepts, about 200 inches
    assert dpi < RESOLUTION_DPI
    assert 14_400 / 72 * dpi <= MAX_PAGE_PIXELS + 1


def test_a_zero_sized_page_does_not_divide_by_zero() -> None:
    assert render_dpi(0, 0) == RESOLUTION_DPI


# --- workbooks that unpack to far more than they weigh ------------------------------------------


def zip_of(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_zip_bomb_is_refused_before_openpyxl_sees_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nbs_workbook, "MAX_UNPACKED_BYTES", 100_000)
    bomb = zip_of({"[Content_Types].xml": b"0" * 5_000_000})
    assert len(bomb) < 20_000  # tiny on the wire, huge when unpacked
    with pytest.raises(NbsParseError, match="unreasonable size"):
        check_zip_size(bomb)


def test_a_workbook_with_thousands_of_members_is_refused() -> None:
    many = zip_of({f"xl/part{n}.xml": b"x" for n in range(nbs_workbook.MAX_ZIP_MEMBERS + 1)})
    with pytest.raises(NbsParseError):
        check_zip_size(many)


def test_a_small_zip_and_a_non_zip_pass_the_check() -> None:
    check_zip_size(zip_of({"[Content_Types].xml": b"<Types/>"}))
    check_zip_size(b"<html>maintenance</html>")  # the parser reports this one itself


def test_an_oversized_workbook_inside_a_download_zip_is_not_unpacked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nbs, "MAX_UNPACKED_BYTES", 100_000)
    download = zip_of({"PMS_OCT_2024.xlsx": b"0" * 5_000_000})
    with pytest.raises(NbsParseError, match="unreasonably large"):
        nbs.workbook_bytes(download)


# --- deployment defaults ------------------------------------------------------------------------


def test_the_image_defaults_to_production_so_a_forgotten_variable_fails_closed() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ENV ENV=production" in dockerfile


def test_the_development_database_is_not_published_on_every_interface() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    for port in ("5432", "9000", "9001"):
        assert f'"{port}:{port}"' not in compose, port


def test_openpyxl_reads_workbooks_through_defusedxml() -> None:
    """Without defusedxml, a crafted xlsx can expand XML entities (memory bomb, local file read)."""
    import openpyxl.xml

    assert openpyxl.xml.DEFUSEDXML
