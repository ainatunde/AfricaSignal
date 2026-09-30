"""OCR for PDFs that have no text layer (NERC's monthly tariff schedules are drawn, not typed).

Pages are rendered with pdfplumber (pypdfium2) and read by the ``tesseract`` command. Automatic
page layout (``--psm 3``) is used because it reads the coloured tables; the single-block mode
(``--psm 6``) skips them. Tesseract's per-word confidence is kept so a parser can refuse a number
it was unsure of: OCR text is evidence for a person to check, not something to trust blindly.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field

import pdfplumber
import pypdfium2 as pdfium

log = logging.getLogger("africasignal.evidence.ocr")

RESOLUTION_DPI = 200
MAX_PAGES = 40
PAGE_TIMEOUT_SECONDS = 120
# Numbers below this confidence (0-100) make their line doubtful.
MIN_WORD_CONFIDENCE = 80.0
# A PDF with fewer typed characters per page than this is treated as having no text layer.
MIN_CHARS_PER_PAGE = 100
# A word that is a number ('209.50', '1,369', '15.9%'), not a label such as 'MD1'.
_NUMBER = re.compile(r"^[\d.,]+%?$")


class OcrUnavailable(RuntimeError):
    """The ``tesseract`` command is not installed."""


class OcrError(RuntimeError):
    """Tesseract failed on a page."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    pages: int
    # Lines (as they appear in ``text``) holding a number read with low confidence. Misread words
    # do no harm to the tariff parser; a misread digit does.
    doubtful_lines: frozenset[str] = field(default_factory=frozenset)


def has_text_layer(content: bytes) -> bool:
    """Whether the PDF has typed text worth using: at least ``MIN_CHARS_PER_PAGE`` characters per
    page (the drawn NERC schedules hold a few stray symbols). Read with pypdfium2, which is fast
    even on a PDF of page-sized images."""
    pdf = pdfium.PdfDocument(content)
    try:
        chars = 0
        for page in pdf:
            textpage = page.get_textpage()
            chars += len(textpage.get_text_range().strip())
            textpage.close()
            page.close()
        return chars >= MIN_CHARS_PER_PAGE * max(len(pdf), 1)
    finally:
        pdf.close()


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _read_page(png: bytes) -> tuple[list[str], set[str]]:
    """The lines of one page image and the ones holding a low-confidence word."""
    exe = shutil.which("tesseract")
    if exe is None:
        raise OcrUnavailable("tesseract is not installed")
    try:
        done = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
            [exe, "stdin", "stdout", "--psm", "3", "-l", "eng", "tsv"],
            input=png,
            capture_output=True,
            timeout=PAGE_TIMEOUT_SECONDS,
            env={"OMP_THREAD_LIMIT": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"tesseract took more than {PAGE_TIMEOUT_SECONDS}s on a page") from exc
    if done.returncode != 0:
        raise OcrError(f"tesseract failed: {done.stderr.decode(errors='replace')[:200]}")

    rows = csv.DictReader(
        io.StringIO(done.stdout.decode("utf-8", errors="replace")),
        delimiter="\t",
        quoting=csv.QUOTE_NONE,
    )
    lines: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
    for row in rows:
        word = (row.get("text") or "").strip()
        if row.get("level") != "5" or not word:
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        lines.setdefault(key, []).append((word, float(row["conf"])))
    ordered = sorted(lines, key=lambda k: tuple(int(p) for p in k))
    text_lines: list[str] = []
    doubtful: set[str] = set()
    for key in ordered:
        line = " ".join(w for w, _ in lines[key])
        text_lines.append(line)
        if any(conf < MIN_WORD_CONFIDENCE for w, conf in lines[key] if _NUMBER.match(w)):
            doubtful.add(line)
    return text_lines, doubtful


def ocr_pdf(content: bytes, *, max_pages: int = MAX_PAGES) -> OcrResult:
    """Read every page (up to ``max_pages``) of a PDF with tesseract."""
    all_lines: list[str] = []
    doubtful: set[str] = set()
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages = pdf.pages[:max_pages]
        for number, page in enumerate(pages, start=1):
            buffer = io.BytesIO()
            page.to_image(resolution=RESOLUTION_DPI).original.save(buffer, format="PNG")
            lines, bad = _read_page(buffer.getvalue())
            all_lines.extend(lines)
            all_lines.append("")  # blank line between pages
            doubtful |= bad
            log.debug("ocr page %d/%d: %d lines", number, len(pages), len(lines))
        total = len(pdf.pages)
    if total > max_pages:
        log.warning("ocr stopped after %d of %d pages", max_pages, total)
    return OcrResult(
        text="\n".join(all_lines).strip(), pages=len(pages), doubtful_lines=frozenset(doubtful)
    )
