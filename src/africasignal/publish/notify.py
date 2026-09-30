"""In-site notifications for followers, and the correction emails that go with them (AS-031).

Wiring (done by the publication code, not here): when a version is published, call
``enqueue_notify_followers(session, version.id)``; for a correction, pass ``kind="correction"``.
``notify_followers`` then

1. creates one notification per follower (dedupe key ``user:version:kind``, so running it twice
   creates nothing new),
2. queues a correction email for followers who opted in to email, for corrections and
   withdrawals only (a new version shows on the site and in the weekly digest),
3. cancels the pending notifications and emails of the version this one supersedes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal.jobs import queue
from africasignal.models import (
    AppUser,
    AssessmentVersion,
    Follow,
    Notification,
    Outbox,
    Situation,
)
from africasignal.publish.outbox import enqueue_email

log = logging.getLogger("africasignal.notify")

KINDS = ("new_version", "correction", "withdrawal")
EMAIL_KINDS = ("correction", "withdrawal")


def dedupe_key(user_id: int, version_id: int, kind: str) -> str:
    return f"{user_id}:{version_id}:{kind}"


def enqueue_notify_followers(
    session: Session, version_id: int, kind: str | None = None
) -> int | None:
    """Queue the job for a published version. Safe to call twice for the same version."""
    payload: dict[str, Any] = {"version_id": version_id}
    if kind is not None:
        payload["kind"] = kind
    return queue.enqueue(
        session, "notify_followers", payload, dedupe_key=f"notify_followers:{version_id}"
    )


def queue_notifications(session: Session, version_id: int, kind: str) -> None:
    """The publication hook: publishing a version queues its follower notifications. The worker
    registers it (see ``jobs/handlers/notify_followers.py``)."""
    enqueue_notify_followers(session, version_id, kind)


def item_snapshot(situation: Situation, version: AssessmentVersion) -> dict[str, Any]:
    """The display text an email needs, copied into the outbox payload."""
    return {
        "slug": situation.slug,
        "title": situation.title,
        "headline": version.headline,
        "scope_label": version.scope_label,
        "change_summary": version.change_summary,
    }


@dataclass
class NotifyResult:
    created: int = 0
    emails: int = 0
    cancelled: int = 0
    skipped: str | None = None


def notify_followers(
    session: Session, version_id: int, now: datetime, kind: str | None = None
) -> NotifyResult:
    """Create notifications for a version's followers. The caller commits."""
    version = session.get(AssessmentVersion, version_id)
    if version is None:
        return NotifyResult(skipped="version does not exist")
    if version.status not in ("published", "withdrawn"):
        return NotifyResult(skipped=f"version is {version.status}")
    if kind is None:
        kind = "withdrawal" if version.status == "withdrawn" else "new_version"
    if kind not in KINDS:
        raise ValueError(f"unknown notification kind {kind!r}")
    situation = session.get(Situation, version.situation_id)
    assert situation is not None  # foreign key

    result = NotifyResult()
    followers = session.execute(
        select(AppUser.id, AppUser.digest_opt_in, AppUser.email_verified_at)
        .join(Follow, Follow.user_id == AppUser.id)
        .where(Follow.situation_id == situation.id, AppUser.deleted_at.is_(None))
        .order_by(AppUser.id)
    ).all()
    for user_id, opted_in, verified_at in followers:
        key = dedupe_key(user_id, version.id, kind)
        created = session.execute(
            pg_insert(Notification)
            .values(user_id=user_id, assessment_version_id=version.id, kind=kind, dedupe_key=key)
            .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
            .returning(Notification.id)
        ).scalar_one_or_none()
        if created is None:
            continue
        result.created += 1
        if kind in EMAIL_KINDS and opted_in and verified_at is not None:
            queued = enqueue_email(
                session,
                "email_correction",
                {
                    "user_id": user_id,
                    "assessment_version_id": version.id,
                    "notification_kind": kind,
                    "item": item_snapshot(situation, version),
                },
                dedupe_key=f"email_correction:{key}",
            )
            result.emails += queued is not None

    if version.supersedes_id is not None:
        result.cancelled = cancel_superseded(session, version.supersedes_id, now)
    return result


def cancel_superseded(session: Session, version_id: int, now: datetime) -> int:
    """Withdraw what was going to tell people about ``version_id``, once it is superseded
    (plan B9 step 4): unread notifications get ``cancelled_at``, and emails not yet sent become
    ``dead`` with reason ``superseded``. Returns the number of notifications cancelled. The caller
    commits."""
    cancelled = session.execute(
        update(Notification)
        .where(
            Notification.assessment_version_id == version_id,
            Notification.cancelled_at.is_(None),
            Notification.read_at.is_(None),
        )
        .values(cancelled_at=now)
    ).rowcount  # type: ignore[attr-defined]
    session.execute(
        update(Outbox)
        .where(
            Outbox.payload["assessment_version_id"].as_integer() == version_id,
            Outbox.status.in_(("pending", "failed")),
        )
        .values(status="dead", last_error="superseded")
    )
    return int(cancelled)


def unread_notifications(session: Session, user_id: int) -> list[Notification]:
    """The "since your last visit" list: not read, not cancelled, newest first."""
    return list(
        session.scalars(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.cancelled_at.is_(None),
            )
            .order_by(Notification.created_at.desc(), Notification.id.desc())
        )
    )


def mark_all_read(session: Session, user_id: int, now: datetime) -> int:
    result = session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=now)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
