"""The NERC adapter against the real saved listing and PDFs (no network)."""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.evidence import ocr as ocr_module
from africasignal.evidence.capture import capture
from africasignal.evidence.ocr import OcrResult, OcrUnavailable
from africasignal.models import Claim, EvidenceDocument, Job, ReportingOrigin, Source
from africasignal.sources import nerc
from africasignal.sources.nerc import (
    EXTRACTOR_VERSION,
    MISMATCH,
    NercAdapter,
    NercError,
    reconcile_tariff_claims,
)
from africasignal.storage import S3Store
from tests.integration.adapter_support import FakeSite, add_source, ctx, store_fixture
from tests.unit.sources.nerc_fixtures import (
    NERC,
    ORDER_PDF,
    ORDER_URL,
    TARIFF_PDF,
    TARIFF_URL,
    needs_tesseract,
    read,
    tariff_ocr,
)

LISTING_URL = "https://nerc.gov.ng/resource-category/orders/"
IKEJA = "electricity_tariff_band_a:ikeja-electric"


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from store_fixture()


@pytest.fixture
def source(session: Session) -> Source:
    return add_source(
        session,
        "nerc",
        adapter="nerc",
        home_url="https://nerc.gov.ng",
        feed_url=LISTING_URL,
    )


@pytest.fixture
def site() -> FakeSite:
    site = FakeSite()
    site.serve(LISTING_URL, (NERC / "orders.html").read_bytes(), "text/html")
    site.serve(TARIFF_URL, read(TARIFF_PDF), "application/pdf")
    site.serve(ORDER_URL, read(ORDER_PDF), "application/pdf")
    return site


def _document(
    session: Session, store: S3Store, source: Source, site: FakeSite, url: str, title: str
) -> EvidenceDocument:
    return capture(session, store, source, url, fetch=site, title=title)


_SEEN: dict[tuple[bytes, str], object] = {}


@pytest.fixture(autouse=True)
def quick_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """pdfplumber takes seconds over the 5 MB schedule; read each file's text once per run."""
    from africasignal.evidence import capture as capture_module

    real = capture_module.extract_text

    def memo(content: bytes, mime: str):  # type: ignore[no-untyped-def]
        key = (content[:4096] + str(len(content)).encode(), mime)
        if key not in _SEEN:
            _SEEN[key] = real(content, mime)
        return _SEEN[key]

    monkeypatch.setattr(capture_module, "extract_text", memo)


@pytest.fixture
def cached_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse the OCR of the real tariff PDF instead of reading its 12 pages in every test."""
    real = ocr_module.ocr_pdf

    def fake(content: bytes, **kwargs: object) -> OcrResult:
        return tariff_ocr() if content == read(TARIFF_PDF) else real(content, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(nerc, "ocr_pdf", fake)


# --- discovery ---------------------------------------------------------------------------------


def test_discovery_lists_every_order_with_its_pdf(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    items = NercAdapter(fetch=site).discover(source, ctx(session, store))
    assert len(items) == 10
    assert TARIFF_URL in {i.url for i in items}
    [(url, kwargs)] = site.calls
    assert url == LISTING_URL
    assert kwargs["max_requests_per_hour"] == 30
    assert kwargs["max_bytes"] == nerc.LISTING_MAX_BYTES


def test_orders_already_captured_are_not_listed_again(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    _document(session, store, source, site, ORDER_URL, "Revised Order")
    urls = {i.url for i in NercAdapter(fetch=site).discover(source, ctx(session, store))}
    assert ORDER_URL not in urls
    assert len(urls) == 9


def test_orders_linking_to_another_site_are_skipped(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    page = (
        (NERC / "orders.html")
        .read_text(encoding="utf-8")
        .replace(
            "https://nerc.gov.ng/wp-content/uploads/2026/09/YEDC_", "https://evil.example/YEDC_"
        )
    )
    site.serve(LISTING_URL, page.encode(), "text/html")
    urls = {i.url for i in NercAdapter(fetch=site).discover(source, ctx(session, store))}
    assert not any("evil.example" in u for u in urls)
    assert len(urls) == 9


def test_a_listing_with_no_orders_means_the_layout_changed(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.serve(LISTING_URL, b"<html><body>Maintenance</body></html>", "text/html")
    with pytest.raises(NercError, match="layout"):
        NercAdapter(fetch=site).discover(source, ctx(session, store))


def test_an_unreachable_listing_is_an_error_carrying_the_reason(
    session: Session, store: S3Store, source: Source
) -> None:
    with pytest.raises(NercError, match="HTTP 404"):
        NercAdapter(fetch=FakeSite()).discover(source, ctx(session, store))


# --- processing --------------------------------------------------------------------------------


@needs_tesseract
def test_the_drawn_tariff_schedule_gives_band_a_claims_read_in_code(
    session: Session, store: S3Store, source: Source, site: FakeSite, cached_ocr: None
) -> None:
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    assert doc.text_content is None or len(doc.text_content.strip()) < 100  # no text layer

    result = NercAdapter().process(doc, ctx(session, store))
    assert result.claims == 3
    assert "read by OCR (12 pages)" in result.notes

    claims = list(
        session.scalars(
            select(Claim).where(Claim.evidence_document_id == doc.id).order_by(Claim.occurred_from)
        )
    )
    assert [(c.stated_value, c.direction) for c in claims] == [
        (Decimal("225.00"), "unknown"),
        (Decimal("206.80"), "down"),
        (Decimal("209.50"), "up"),
    ]
    current = claims[-1]
    assert (current.occurred_from, current.occurred_to) == (date(2024, 8, 1), date(2026, 9, 30))
    assert current.time_precision == "month"
    assert {c.policy_series for c in claims} == {IKEJA}
    assert {(c.claim_type, c.stated_unit, c.valid) for c in claims} == {
        ("policy_statement", "NGN/kWh", True)
    }
    assert {c.extractor_version for c in claims} == {EXTRACTOR_VERSION}
    assert current.passage == "A - Non-MD 225.00 206.80 209.50"


@needs_tesseract
def test_the_ocr_text_is_kept_on_the_document_and_the_passage_is_in_it(
    session: Session, store: S3Store, source: Source, site: FakeSite, cached_ocr: None
) -> None:
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    NercAdapter().process(doc, ctx(session, store))
    assert doc.text_content is not None and "A - Non-MD 225.00 206.80 209.50" in doc.text_content
    assert doc.simhash is not None
    claim = session.scalars(select(Claim).where(Claim.evidence_document_id == doc.id)).first()
    assert claim is not None and claim.passage_start is not None and claim.passage_end is not None
    assert doc.text_content[claim.passage_start : claim.passage_end] == claim.passage


@needs_tesseract
def test_the_ocr_text_is_not_kept_when_the_permission_forbids_full_text(
    session: Session, store: S3Store, site: FakeSite, cached_ocr: None
) -> None:
    source = add_source(
        session, "nerc2", adapter="nerc", home_url="https://nerc.gov.ng", may_store_full_text=False
    )
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    NercAdapter().process(doc, ctx(session, store))
    assert doc.text_content is None


@needs_tesseract
def test_processing_twice_stores_the_claims_once(
    session: Session, store: S3Store, source: Source, site: FakeSite, cached_ocr: None
) -> None:
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    NercAdapter().process(doc, ctx(session, store))
    again = NercAdapter().process(doc, ctx(session, store))
    assert again.claims == 0
    assert "already stored" in " ".join(again.notes)
    assert (
        len(list(session.scalars(select(Claim).where(Claim.evidence_document_id == doc.id)))) == 3
    )


@needs_tesseract
def test_the_document_gets_a_primary_document_origin(
    session: Session, store: S3Store, source: Source, site: FakeSite, cached_ocr: None
) -> None:
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    NercAdapter().process(doc, ctx(session, store))
    origin = session.get(ReportingOrigin, doc.origin_id)
    assert origin is not None and origin.kind == "primary_document"


@needs_tesseract
def test_a_disco_without_a_policy_series_gets_no_claims(
    session: Session, store: S3Store, source: Source, site: FakeSite, cached_ocr: None
) -> None:
    doc = _document(session, store, source, site, TARIFF_URL, "JED MYTO SEPTEMBER 2026")
    result = NercAdapter().process(doc, ctx(session, store))
    assert result.claims == 0
    assert any("jos-electricity" in n and "policies.yaml" in n for n in result.notes)


def test_a_plain_order_is_read_from_its_text_layer_and_makes_no_tariff_claims(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    doc = _document(session, store, source, site, ORDER_URL, "Revised Order on DisCos")
    result = NercAdapter().process(doc, ctx(session, store))
    assert result.claims == 0
    assert "read by text layer" in result.notes
    assert any("not a DisCo tariff order" in n for n in result.notes)
    queued = session.scalars(select(Job).where(Job.kind == "extract_claims")).all()
    assert [j.payload for j in queued] == [{"document_id": doc.id}]  # the model reads its policy
    assert doc.text_content and "ORDER NO: NERC/2026/062A" in doc.text_content


def test_a_table_read_with_doubt_makes_no_claims(
    session: Session,
    store: S3Store,
    source: Source,
    site: FakeSite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        tariff_ocr().text
        if False
        # built by hand so the test needs no tesseract
        else (
            "Table - 3: Approved Allowed Tariffs (&/kWh) for the YTTS under IE\n"
            "Tariff Class Apr 2024 | May - Jul 2024 | Aug 2024 - Sep 2026\n"
            "A - Non-MD 225.00 206.80 209.50\n"
        )
    )
    row = "A - Non-MD 225.00 206.80 209.50"
    monkeypatch.setattr(
        nerc, "ocr_pdf", lambda content, **kw: OcrResult(text, 12, frozenset({row}))
    )
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    result = NercAdapter().process(doc, ctx(session, store))
    assert result.claims == 0
    assert any("read with doubt" in n for n in result.notes)


def test_a_clean_hand_built_table_gives_claims_without_tesseract(
    session: Session,
    store: S3Store,
    source: Source,
    site: FakeSite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "This Order shall take effect on 1 September 2026.\n\n"
        "Table - 3: Approved Allowed Tariffs (&/kWh) for the YTTS under IE\n"
        "Tariff Class Apr 2024 | May - Jul 2024 | Aug 2024 - Sep 2026\n"
        "A - Non-MD 225.00 206.80 209.50\n"
    )
    monkeypatch.setattr(nerc, "ocr_pdf", lambda content, **kw: OcrResult(text, 12))
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    assert NercAdapter().process(doc, ctx(session, store)).claims == 3


def test_a_band_a_sentence_in_a_typed_order_becomes_one_claim_with_the_effective_date(
    session: Session,
    store: S3Store,
    source: Source,
    site: FakeSite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "This Order shall take effect on 1st October 2026. "
        "The tariff for Band A customers of Ikeja Electric shall be ₦215.00/kWh. "
        "Band B customers pay N63.17 per kWh."
    )
    monkeypatch.setattr(nerc, "ocr_pdf", lambda content, **kw: OcrResult(text, 1))
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO OCTOBER 2026")
    result = NercAdapter().process(doc, ctx(session, store))
    assert result.claims == 1
    claim = session.scalars(select(Claim).where(Claim.evidence_document_id == doc.id)).one()
    assert claim.stated_value == Decimal("215.00")
    assert (claim.occurred_from, claim.time_precision) == (date(2026, 10, 1), "day")
    assert (
        claim.passage == "The tariff for Band A customers of Ikeja Electric shall be ₦215.00/kWh."
    )


def test_a_missing_tesseract_fails_the_job_so_the_source_shows_as_degraded(
    session: Session,
    store: S3Store,
    source: Source,
    site: FakeSite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(content: bytes, **kw: object) -> OcrResult:
        raise OcrUnavailable("tesseract is not installed")

    monkeypatch.setattr(nerc, "ocr_pdf", unavailable)
    doc = _document(session, store, source, site, TARIFF_URL, "IE MYTO SEPTEMBER 2026")
    with pytest.raises(OcrUnavailable):
        NercAdapter().process(doc, ctx(session, store))


def test_a_document_that_is_not_a_pdf_is_left_alone(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    doc = _document(session, store, source, site, LISTING_URL, "The listing")
    result = NercAdapter().process(doc, ctx(session, store))
    assert result.claims == 0 and "not a PDF" in result.notes[0]


# --- checking a language model's tariff claims against the code -------------------------------


def _model_claim(doc: EvidenceDocument, value: str, series: str = IKEJA, **kw: object) -> Claim:
    fields: dict[str, object] = {
        "evidence_document_id": doc.id,
        "claim_type": "policy_statement",
        "text": "Band A tariff",
        "passage": "Band A customers pay",
        "policy_series": series,
        "stated_value": Decimal(value),
        "extractor_version": "claim_extract_v1+test",
        "valid": True,
    }
    fields.update(kw)
    return Claim(**fields)


def test_a_model_tariff_the_code_cannot_find_in_the_document_is_made_invalid(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    doc = _document(session, store, source, site, ORDER_URL, "Order")
    text = "Band A customers shall pay ₦209.50/kWh from September.\nLifeline is N4 per kWh."
    agree, disagree, other = (
        _model_claim(doc, "209.50"),
        _model_claim(doc, "290.50"),
        _model_claim(doc, "999", series="pms_regulated_price"),
    )
    session.add_all([agree, disagree, other])
    session.flush()
    assert reconcile_tariff_claims(session, doc, text) == 1
    assert (agree.valid, disagree.valid, other.valid) == (True, False, True)
    assert disagree.invalid_reason == MISMATCH


def test_reconciling_leaves_the_claims_code_made_and_the_already_invalid_alone(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    doc = _document(session, store, source, site, ORDER_URL, "Order")
    by_code = _model_claim(doc, "999", extractor_version=EXTRACTOR_VERSION)
    already = _model_claim(doc, "998", valid=False, invalid_reason="passage_not_found")
    session.add_all([by_code, already])
    session.flush()
    assert reconcile_tariff_claims(session, doc, "Band A pays ₦209.50/kWh") == 0
    assert by_code.valid and already.invalid_reason == "passage_not_found"
