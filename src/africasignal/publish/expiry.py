"""Expiry: assessments past ``valid_until`` become stale (spec B9, AS-013)."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.jobs import queue
from africasignal.models import AssessmentVersion, Situation

log = logging.getLogger("africasignal.publish.expiry")


def expire_assessments(session: Session, now: datetime) -> list[int]:
    """Mark current published versions past ``valid_until`` as ``stale`` and queue a
    re-assessment for each situation. Returns the situation ids.

    Stale is not hidden: the page shows "Out of date: last checked {date}". A re-assessment with
    the same inputs stores nothing new, so a version stays stale until newer data arrives.
    """
    rows = session.execute(
        select(Situation, AssessmentVersion)
        .join(AssessmentVersion, AssessmentVersion.id == Situation.current_version_id)
        .where(
            AssessmentVersion.status == "published",
            AssessmentVersion.valid_until.is_not(None),
            AssessmentVersion.valid_until < now,
        )
        .order_by(Situation.id)
    ).all()
    expired: list[int] = []
    for situation, version in rows:
        version.status = "stale"
        queue.enqueue(
            session,
            "assess_situation",
            {"situation_id": situation.id},
            dedupe_key=f"assess_situation:{situation.id}:expire:{version.id}",
        )
        expired.append(situation.id)
    session.flush()
    if expired:
        log.info("expired %d assessments", len(expired))
    return expired
