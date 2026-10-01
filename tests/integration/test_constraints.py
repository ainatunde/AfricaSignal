from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from africasignal.models import EvidenceDocument, Job, Measurement, Place, Series, Source


def _now() -> datetime:
    return datetime.now(UTC)


def _source(session: Session) -> Source:
    source = Source(
        slug="nbs-elibrary",
        name="NBS",
        kind="official_statistics",
        adapter="nbs",
        schedule_minutes=1440,
    )
    session.add(source)
    session.flush()
    return source


def _document(session: Session, source: Source, sha: str = "a" * 64) -> EvidenceDocument:
    doc = EvidenceDocument(
        source_id=source.id,
        url="https://example.org/a",
        canonical_url="https://example.org/a",
        retrieved_at=_now(),
        content_sha256=sha,
        storage_key=f"evidence/aa/aa/{sha}.html",
        mime="text/html",
    )
    session.add(doc)
    session.flush()
    return doc


def test_job_dedupe_key_is_unique(session: Session) -> None:
    session.add(Job(kind="fetch_source", dedupe_key="fetch:1:slot"))
    session.flush()
    session.add(Job(kind="fetch_source", dedupe_key="fetch:1:slot"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_jobs_without_dedupe_key_are_not_deduplicated(session: Session) -> None:
    session.add_all([Job(kind="x"), Job(kind="x")])
    session.flush()


def test_evidence_document_unique_per_source_url_and_hash(session: Session) -> None:
    source = _source(session)
    _document(session, source)
    with session.begin_nested():
        with pytest.raises(IntegrityError):
            _document(session, source)
    # Changed bytes for the same URL is allowed: it is a new document.
    _document(session, source, sha="b" * 64)


def test_measurement_unique_per_series_place_period_vintage(session: Session) -> None:
    source = _source(session)
    doc = _document(session, source)
    series = Series(
        item_code="pms_litre",
        topic="energy",
        source_id=source.id,
        unit="NGN/litre",
        frequency="monthly",
    )
    place = Place(kind="country", name="Nigeria", code="NG")
    session.add_all([series, place])
    session.flush()

    def measurement(vintage: date) -> Measurement:
        return Measurement(
            series_id=series.id,
            place_id=place.id,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            value=Decimal("870.50"),
            vintage=vintage,
            evidence_document_id=doc.id,
        )

    session.add(measurement(date(2026, 9, 15)))
    session.flush()
    session.add(measurement(date(2026, 10, 15)))  # a revision is a new vintage
    session.flush()
    session.add(measurement(date(2026, 9, 15)))
    with pytest.raises(IntegrityError):
        session.flush()
