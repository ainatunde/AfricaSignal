"""The price announcement adapter: NNPC's real saved feed, and synthetic pages for what it finds."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.evidence.capture import capture
from africasignal.models import Claim, EvidenceDocument, Place, ReportingOrigin, Source
from africasignal.sources.base import get_adapter
from africasignal.sources.price_announcements import (
    EXTRACTOR_VERSION,
    AnnouncementError,
    PriceAnnouncementAdapter,
    UnsupportedSource,
)
from africasignal.storage import S3Store
from tests.integration.adapter_support import (
    FakeSite,
    add_country,
    add_source,
    ctx,
    store_fixture,
)
from tests.unit.test_fixture_manifests import FIXTURES

FEED = "https://fde-nnpc-web-cms-prod-dwfrd0hraahrbhhg.a02.azurefd.net/api/posts"
FEED_URL = FEED + "?sort=publishedAt:desc&pagination[pageSize]=25"
PAGE = "https://nnpcgroup.com/insights/nnpc-retail-adjusts-pump-prices"

PAGE_HTML = """<html><head><title>NNPC Retail adjusts pump prices</title></head><body><article>
<h1>NNPC Retail adjusts pump prices</h1>
<p>NNPC Retail has reduced the pump price of petrol from ₦1,050 to ₦980 per litre effective 5
October 2026 at all its retail outlets nationwide.</p>
<p>Diesel now sells at ₦1,150 per litre at NNPC stations.</p>
<p>NNPC Limited thanks its customers for their patience.</p>
</article></body></html>"""


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from store_fixture()


@pytest.fixture
def source(session: Session) -> Source:
    # the seeded permission: link and 300-character quotation, no full text
    return add_source(
        session,
        "nnpc",
        adapter="price_announcement",
        home_url="https://nnpcgroup.com",
        feed_url=FEED,
        kind="company",
        may_store_full_text=False,
        max_quote_chars=300,
    )


@pytest.fixture
def site() -> FakeSite:
    site = FakeSite()
    site.serve(FEED_URL, (FIXTURES / "nnpc" / "posts_newest.json").read_bytes(), "application/json")
    site.serve(PAGE, PAGE_HTML.encode(), "text/html")
    return site


def _page_document(session: Session, store: S3Store, source: Source, site: FakeSite):  # type: ignore[no-untyped-def]
    return capture(
        session,
        store,
        source,
        PAGE,
        fetch=site,
        title="NNPC Retail adjusts pump prices",
        published_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_the_adapter_is_registered_under_the_name_the_sources_use() -> None:
    from africasignal.jobs import handlers

    handlers.load_all()
    assert isinstance(get_adapter("price_announcement"), PriceAnnouncementAdapter)
    assert get_adapter("nerc") is not None


# --- discovery ---------------------------------------------------------------------------------


def test_the_ten_newest_real_posts_list_nothing_because_none_is_about_a_fuel_price(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    assert PriceAnnouncementAdapter(fetch=site).discover(source, ctx(session, store)) == []
    [(url, kwargs)] = site.calls
    assert url == FEED_URL
    assert kwargs["max_requests_per_hour"] == 30


def _feed_with_price_post() -> bytes:
    data = json.loads((FIXTURES / "nnpc" / "posts_newest.json").read_text(encoding="utf-8"))
    data["data"].insert(
        0,
        {
            "id": 1,
            "title": "NNPC Retail adjusts pump prices",
            "slug": "nnpc-retail-adjusts-pump-prices",
            "content": "<p>The pump price of petrol is now <b>₦980</b> per litre.</p>",
            "publishedAt": "2026-10-01T08:00:00.000Z",
        },
    )
    return json.dumps(data).encode()


def test_a_post_about_a_fuel_price_is_listed_with_its_page_url(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.serve(FEED_URL, _feed_with_price_post(), "application/json")
    [item] = PriceAnnouncementAdapter(fetch=site).discover(source, ctx(session, store))
    assert item.url == PAGE
    assert item.title == "NNPC Retail adjusts pump prices"
    assert item.published_at == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def test_a_post_already_captured_is_not_listed_again(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.serve(FEED_URL, _feed_with_price_post(), "application/json")
    _page_document(session, store, source, site)
    assert PriceAnnouncementAdapter(fetch=site).discover(source, ctx(session, store)) == []


def test_the_feed_json_is_not_stored_as_evidence(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.serve(FEED_URL, _feed_with_price_post(), "application/json")
    PriceAnnouncementAdapter(fetch=site).discover(source, ctx(session, store))
    assert session.scalars(select(EvidenceDocument)).first() is None


def test_an_unreachable_feed_is_an_error_carrying_the_reason(
    session: Session, store: S3Store, source: Source
) -> None:
    with pytest.raises(AnnouncementError, match="HTTP 404"):
        PriceAnnouncementAdapter(fetch=FakeSite()).discover(source, ctx(session, store))


def test_a_source_without_a_feed_url_cannot_be_discovered(
    session: Session, store: S3Store, site: FakeSite
) -> None:
    source = add_source(
        session, "nnpc2", adapter="price_announcement", home_url="https://nnpcgroup.com"
    )
    with pytest.raises(AnnouncementError, match="feed_url"):
        PriceAnnouncementAdapter(fetch=site).discover(source, ctx(session, store))


def test_nmdpra_is_refused_by_name_because_its_site_has_nothing_to_read(
    session: Session, store: S3Store, site: FakeSite
) -> None:
    source = add_source(
        session, "nmdpra", adapter="price_announcement", home_url="https://nmdpra.gov.ng"
    )
    with pytest.raises(UnsupportedSource, match="nmdpra"):
        PriceAnnouncementAdapter(fetch=site).discover(source, ctx(session, store))
    assert site.calls == []


# --- processing --------------------------------------------------------------------------------


def test_a_page_with_petrol_and_diesel_prices_becomes_a_policy_claim_and_a_price_claim(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    country = add_country(session)
    doc = _page_document(session, store, source, site)
    assert doc.text_content is None  # the permission forbids keeping the text

    result = PriceAnnouncementAdapter().process(doc, ctx(session, store))
    assert result.claims == 2
    claims = {
        c.stated_value: c
        for c in session.scalars(select(Claim).where(Claim.evidence_document_id == doc.id))
    }
    petrol, diesel = claims[Decimal("980")], claims[Decimal("1150")]

    assert (petrol.claim_type, petrol.policy_series, petrol.item_code) == (
        "policy_statement",
        "pms_regulated_price",
        None,
    )
    assert (petrol.direction, petrol.occurred_from, petrol.time_precision) == (
        "down",
        date(2026, 10, 5),
        "day",
    )
    assert petrol.stated_unit == "NGN/litre" and petrol.valid
    assert "₦980 per litre" in petrol.passage

    assert (diesel.claim_type, diesel.item_code, diesel.policy_series) == (
        "price_statement",
        "ago_litre",
        None,
    )
    assert diesel.occurred_from == date(2026, 10, 1)  # no effective date: the publication day
    assert (diesel.place_id, diesel.place_precision) == (country.id, "national")
    assert {c.extractor_version for c in claims.values()} == {EXTRACTOR_VERSION}


def test_every_passage_is_within_the_permissions_quotation_limit_and_in_the_page(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    from africasignal.evidence.text import extract_text

    doc = _page_document(session, store, source, site)
    PriceAnnouncementAdapter().process(doc, ctx(session, store))
    text = extract_text(PAGE_HTML.encode(), "text/html").text
    assert text
    for claim in session.scalars(select(Claim).where(Claim.evidence_document_id == doc.id)):
        assert len(claim.passage) <= 300
        assert " ".join(claim.passage.split()) in " ".join(text.split())


def test_a_source_that_may_not_be_quoted_gets_no_claims_because_there_is_no_passage_to_keep(
    session: Session, store: S3Store, site: FakeSite
) -> None:
    source = add_source(
        session,
        "nnpc3",
        adapter="price_announcement",
        home_url="https://nnpcgroup.com",
        may_store_full_text=False,
        max_quote_chars=0,
    )
    doc = _page_document(session, store, source, site)
    assert PriceAnnouncementAdapter().process(doc, ctx(session, store)).claims == 0


def test_a_shorter_quotation_limit_shortens_the_passage(
    session: Session, store: S3Store, site: FakeSite
) -> None:
    source = add_source(
        session,
        "nnpc4",
        adapter="price_announcement",
        home_url="https://nnpcgroup.com",
        may_store_full_text=False,
        max_quote_chars=60,
    )
    doc = _page_document(session, store, source, site)
    PriceAnnouncementAdapter().process(doc, ctx(session, store))
    passages = list(
        session.scalars(select(Claim.passage).where(Claim.evidence_document_id == doc.id))
    )
    assert passages and all(len(p) <= 60 for p in passages)


def test_processing_twice_stores_the_claims_once(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    doc = _page_document(session, store, source, site)
    PriceAnnouncementAdapter().process(doc, ctx(session, store))
    again = PriceAnnouncementAdapter().process(doc, ctx(session, store))
    assert again.claims == 0 and "already stored" in again.notes[0]
    assert (
        len(list(session.scalars(select(Claim).where(Claim.evidence_document_id == doc.id)))) == 2
    )


def test_the_page_gets_a_primary_document_origin(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    doc = _page_document(session, store, source, site)
    PriceAnnouncementAdapter().process(doc, ctx(session, store))
    origin = session.get(ReportingOrigin, doc.origin_id)
    assert origin is not None and origin.kind == "primary_document"


def test_a_page_without_a_fuel_price_makes_no_claims(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.serve(
        PAGE,
        b"<html><body><article><p>NNPC Limited reported a profit of \xe2\x82\xa67.2 trillion "
        b"and petrol supply improved across the country this year.</p></article></body></html>",
        "text/html",
    )
    doc = _page_document(session, store, source, site)
    result = PriceAnnouncementAdapter().process(doc, ctx(session, store))
    assert result.claims == 0 and "no fuel price statement" in result.notes[0]


def test_a_document_with_no_readable_text_is_left_alone(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    site.serve(PAGE, b"PK\x03\x04 not text", "application/zip")
    doc = _page_document(session, store, source, site)
    result = PriceAnnouncementAdapter().process(doc, ctx(session, store))
    assert result.claims == 0 and "no readable text" in result.notes[0]


def test_claims_carry_no_place_when_the_country_is_not_loaded(
    session: Session, store: S3Store, source: Source, site: FakeSite
) -> None:
    assert session.scalars(select(Place).where(Place.code == "NG")).first() is None
    doc = _page_document(session, store, source, site)
    PriceAnnouncementAdapter().process(doc, ctx(session, store))
    diesel = session.scalars(
        select(Claim).where(Claim.evidence_document_id == doc.id, Claim.item_code == "ago_litre")
    ).one()
    assert (diesel.place_id, diesel.place_precision) == (None, "unknown")
