"""Factories for the email, notification and digest tests."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from africasignal.models import (
    AppUser,
    AssessmentInput,
    AssessmentVersion,
    EvidenceDocument,
    Follow,
    Place,
    Situation,
    Source,
    SourcePermission,
)

LAGOS = timezone(timedelta(hours=1))
_today = datetime.now(LAGOS).date()
_days_until_monday = (7 - _today.weekday()) % 7 or 7
NOW = datetime.combine(
    _today + timedelta(days=_days_until_monday), time(7, 10), tzinfo=LAGOS
)  # next Monday, 07:10 in Lagos keeps database-default queue times due in tests.


def add_place(
    session: Session, code: str, name: str, kind: str, parent: Place | None = None
) -> Place:
    place = Place(kind=kind, name=name, code=code, parent_id=parent.id if parent else None)
    session.add(place)
    session.flush()
    return place


def add_user(
    session: Session, email: str, *, verified: bool = True, digest: bool = False
) -> AppUser:
    user = AppUser(
        email=email,
        email_verified_at=NOW if verified else None,
        digest_opt_in=digest,
        digest_opt_in_at=NOW if digest else None,
    )
    session.add(user)
    session.flush()
    return user


def add_situation(
    session: Session, slug: str, place: Place, topic: str = "energy", title: str | None = None
) -> Situation:
    situation = Situation(
        slug=slug,
        kind="price_series",
        topic=topic,
        title=title or f"Price {slug}",
        item_code=slug,
        place_id=place.id,
    )
    session.add(situation)
    session.flush()
    return situation


def add_version(
    session: Session,
    situation: Situation,
    *,
    version: int = 1,
    status: str = "published",
    severity: str = "medium",
    published_at: datetime | None = NOW,
    supersedes: AssessmentVersion | None = None,
    current: bool = True,
    headline: str | None = None,
    change_summary: str | None = None,
) -> AssessmentVersion:
    row = AssessmentVersion(
        situation_id=situation.id,
        version=version,
        template="T1_price_change",
        template_version="t1",
        policy_version="p1",
        inputs_hash=f"{situation.slug}-{version}",
        status=status,
        evidence_state="reported",
        severity=severity,
        headline=headline or f"{situation.title} rose {version}%",
        scope_label="Lagos State (state average, NBS)",
        period_label="September 2026",
        published_at=published_at,
        change_summary=change_summary,
        supersedes_id=supersedes.id if supersedes else None,
    )
    session.add(row)
    session.flush()
    if current:
        situation.current_version_id = row.id
        session.flush()
    return row


def attach_permissioned_evidence(session: Session, version: AssessmentVersion) -> EvidenceDocument:
    """Give a synthetic published version explicit current evidence and numeric reuse rights."""
    source = Source(
        slug=f"email-test-{version.id}",
        name="Email test source",
        kind="official_statistics",
        adapter="nbs",
        schedule_minutes=1440,
    )
    session.add(source)
    session.flush()
    session.add(
        SourcePermission(
            source_id=source.id,
            version=1,
            may_collect=True,
            may_store_full_text=False,
            max_quote_chars=200,
            may_republish_numbers=True,
            approved_at=NOW,
        )
    )
    document = EvidenceDocument(
        source_id=source.id,
        url="https://source.example/report",
        canonical_url="https://source.example/report",
        retrieved_at=NOW,
        content_sha256=f"{version.id:064d}",
        storage_key=f"test/{version.id}",
        mime="text/html",
        excerpt="An approved source excerpt.",
    )
    session.add(document)
    session.flush()
    session.add(
        AssessmentInput(
            assessment_version_id=version.id,
            input_kind="evidence_document",
            input_id=document.id,
        )
    )
    session.flush()
    return document


def follow(session: Session, user: AppUser, situation: Situation) -> None:
    session.add(Follow(user_id=user.id, situation_id=situation.id))
    session.flush()
