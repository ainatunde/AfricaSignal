"""The weekly email digest (AS-032): Monday 07:00 Africa/Lagos, opted-in users only.

``run_weekly_digest`` builds one digest per user and queues it in the outbox; ``dispatch_outbox``
sends it. The outbox dedupe key ``digest:{user}:{ISO week}`` means a second run in the same week
queues nothing, so each user gets at most one digest per week.

A user with nothing to report gets no email. "Their places" are the places in their preferences
and the states above them (an LGA brings its state); the country is left out so national
situations do not fill every digest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from africasignal.models import (
    AppUser,
    AssessmentVersion,
    Follow,
    Place,
    Preference,
    Situation,
)
from africasignal.publish.notify import item_snapshot
from africasignal.publish.outbox import enqueue_email
from africasignal.publish.suspension import publication_suspended

log = logging.getLogger("africasignal.digest")

LAGOS = ZoneInfo("Africa/Lagos")
WINDOW = timedelta(days=7)
TOP_CHANGES = 3
MATERIAL = ("medium", "high")


def iso_week(now: datetime) -> str:
    """``2026-W40``: the ISO week in Lagos time, the unit of digest deduplication."""
    year, week, _ = now.astimezone(LAGOS).isocalendar()
    return f"{year}-W{week:02d}"


def _place_scope(session: Session, place_ids: list[int]) -> set[int]:
    scope: set[int] = set()
    for place_id in place_ids:
        place = session.get(Place, place_id)
        while place is not None and place.kind != "country" and place.id not in scope:
            scope.add(place.id)
            place = session.get(Place, place.parent_id) if place.parent_id else None
    return scope


def _recent_current(now: datetime) -> Any:
    """Situations whose current version is published and was published in the last 7 days."""
    return (
        select(Situation, AssessmentVersion)
        .join(AssessmentVersion, AssessmentVersion.id == Situation.current_version_id)
        .where(
            AssessmentVersion.status == "published",
            AssessmentVersion.published_at >= now - WINDOW,
            AssessmentVersion.published_at <= now,
        )
    )


@dataclass(frozen=True)
class DigestContent:
    week: str
    followed: list[dict[str, Any]]
    top: list[dict[str, Any]]

    @property
    def empty(self) -> bool:
        return not self.followed and not self.top

    def payload(self, user_id: int) -> dict[str, Any]:
        return {"user_id": user_id, "week": self.week, "followed": self.followed, "top": self.top}


def build_digest(session: Session, user_id: int, now: datetime) -> DigestContent:
    followed_rows = session.execute(
        _recent_current(now)
        .join(Follow, Follow.situation_id == Situation.id)
        .where(Follow.user_id == user_id)
        .order_by(AssessmentVersion.published_at.desc(), Situation.id)
    ).all()
    followed_ids = {situation.id for situation, _ in followed_rows}

    top_rows: list[Any] = []
    preference = session.get(Preference, user_id)
    scope = _place_scope(session, list(preference.place_ids)) if preference else set()
    if scope:
        query = _recent_current(now).where(
            Situation.place_id.in_(scope), AssessmentVersion.severity.in_(MATERIAL)
        )
        if preference and preference.topics:
            query = query.where(Situation.topic.in_(preference.topics))
        if followed_ids:
            query = query.where(Situation.id.notin_(followed_ids))
        top_rows = list(
            session.execute(
                query.order_by(
                    case((AssessmentVersion.severity == "high", 0), else_=1),
                    AssessmentVersion.published_at.desc(),
                    Situation.id,
                ).limit(TOP_CHANGES)
            ).all()
        )
    return DigestContent(
        week=iso_week(now),
        followed=[item_snapshot(s, v) for s, v in followed_rows],
        top=[item_snapshot(s, v) for s, v in top_rows],
    )


@dataclass
class DigestRun:
    queued: int = 0
    already_queued: int = 0
    empty: int = 0
    suspended: bool = False


def run_weekly_digest(session: Session, now: datetime) -> DigestRun:
    """Queue this week's digests. The caller commits. Does nothing while publication is
    suspended: that week's digests are skipped, not delayed."""
    run = DigestRun()
    if publication_suspended(session):
        run.suspended = True
        return run
    week = iso_week(now)
    user_ids = session.scalars(
        select(AppUser.id)
        .where(
            AppUser.digest_opt_in.is_(True),
            AppUser.email_verified_at.is_not(None),
            AppUser.deleted_at.is_(None),
        )
        .order_by(AppUser.id)
    ).all()
    for user_id in user_ids:
        content = build_digest(session, user_id, now)
        if content.empty:
            run.empty += 1
        elif enqueue_email(
            session, "email_digest", content.payload(user_id), dedupe_key=f"digest:{user_id}:{week}"
        ):
            run.queued += 1
        else:
            run.already_queued += 1
    log.info("weekly digest %s: %s", week, run)
    return run
