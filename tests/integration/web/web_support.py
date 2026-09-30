"""Shared setup for the public site and API tests: real NBS petrol and food data, assessed and
published the way AS-012 will, and a test client that uses the test's own database session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import AssessmentVersion, Place, Situation, Source
from africasignal.publish.situations import assess_situation, ensure_situations
from africasignal.storage import S3Store
from tests.integration.nbs_support import import_bytes

PMS_SEP, PMS_OCT = "FUEL_SEPT_2024_REPORT.xlsx", "PMS_OCT_2024_REPORT.xlsx"
FOOD_OCT = "selected_food_oct_2024.xlsx"
ASSESSED_AT = datetime(2024, 11, 25, 12, tzinfo=UTC)  # a week after the October 2024 release
PUBLISHED_AT = datetime(2024, 11, 26, 9, tzinfo=UTC)


def publish(
    session: Session,
    situation: Situation,
    version: AssessmentVersion,
    *,
    published_at: datetime = PUBLISHED_AT,
    valid_until: datetime | None = None,
) -> AssessmentVersion:
    """What the publication policy will do (AS-012): mark a draft published and make it current.
    ``valid_until`` defaults to a year ahead so the page is not stale unless a test says so."""
    version.status = "published"
    version.policy_version = "pp-1"
    version.published_at = published_at
    version.valid_until = valid_until or datetime.now(UTC) + timedelta(days=365)
    situation.current_version_id = version.id
    session.flush()
    return version


def assess_and_publish(
    session: Session, item: str, place_code: str, **kwargs: object
) -> tuple[Situation, AssessmentVersion]:
    place_id = session.scalars(select(Place.id).where(Place.code == place_code)).one()
    (situation,) = ensure_situations(session, [(item, place_id)])
    version = assess_situation(session, situation.id, ASSESSED_AT).version
    assert version is not None
    # Assessed a week after the release, so the state is what the release supports.
    return situation, publish(session, situation, version, **kwargs)  # type: ignore[arg-type]


def seed_petrol(
    session: Session, store: S3Store, source: Source, place_codes: tuple[str, ...] = ("NG-LA", "NG")
) -> dict[str, tuple[Situation, AssessmentVersion]]:
    """September and October 2024 petrol data, with the given places assessed and published."""
    import_bytes(session, store, source, PMS_SEP)
    import_bytes(session, store, source, PMS_OCT)
    return {code: assess_and_publish(session, "pms_litre", code) for code in place_codes}
