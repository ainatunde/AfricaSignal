from africasignal.evidence.text import detect_mime, extension_for, extract_text

HTML = (
    "<html><head><title>Petrol price rises</title></head><body><article>"
    "<h1>Petrol price rises</h1>"
    "<p>The pump price of petrol rose to N870 per litre in Lagos on Monday, according to dealers, "
    "as depot prices climbed again this week.</p>"
    "<p>Marketers said the increase followed a rise in the exchange rate and supply constraints at "
    "several depots across the country.</p></article></body></html>"
)


def make_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF whose text layer contains ``lines``."""
    stream = "BT /F1 12 Tf 72 720 Td 14 TL " + " T* ".join(f"({t}) Tj" for t in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_html_text_and_title_are_extracted() -> None:
    result = extract_text(HTML.encode(), "text/html")
    assert result.text is not None and "N870 per litre" in result.text
    assert result.title == "Petrol price rises"


def test_html_without_readable_text_gives_none() -> None:
    assert extract_text(b"<html><body></body></html>", "text/html").text is None


def test_pdf_text_is_extracted() -> None:
    pdf = make_pdf(["NERC Tariff Order", "Band A rate is N209.50 per kWh"])
    text = extract_text(pdf, "application/pdf").text
    assert text is not None
    assert "NERC Tariff Order" in text and "N209.50 per kWh" in text


def test_broken_pdf_gives_none_instead_of_raising() -> None:
    assert extract_text(b"%PDF-1.4 not really", "application/pdf").text is None


def test_formats_without_prose_give_none() -> None:
    assert extract_text(b"PK\x03\x04", "application/vnd.ms-excel").text is None


def test_detect_mime_prefers_the_header() -> None:
    assert detect_mime("text/html; charset=utf-8", b"", "https://x/a") == "text/html"


def test_detect_mime_falls_back_to_content_then_url() -> None:
    assert detect_mime(None, b"%PDF-1.7", "https://x/a") == "application/pdf"
    assert (
        detect_mime("application/octet-stream", b"<!DOCTYPE html><html>", "https://x/a")
        == "text/html"
    )
    assert detect_mime(None, b"PK", "https://nbs.example/food.xlsx?dl=1").endswith(
        "spreadsheetml.sheet"
    )
    assert detect_mime(None, b"???", "https://x/unknown") == "application/octet-stream"


def test_extension_for() -> None:
    assert extension_for("text/html") == "html"
    assert extension_for("application/pdf") == "pdf"
    assert extension_for("application/x-weird") == "bin"
