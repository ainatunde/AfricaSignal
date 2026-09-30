"""Factories for the email, notification and digest tests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from africasignal.models import AppUser, AssessmentVersion, Follow, Place, Situation

NOW = datetime.fromisoformat("2026-10-05T07:10:00+01:00")  # a Monday, 07:10 in Lagos


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


def follow(session: Session, user: AppUser, situation: Situation) -> None:
    session.add(Follow(user_id=user.id, situation_id=situation.id))
    session.flush()
