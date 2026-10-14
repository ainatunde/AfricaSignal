"""Account operations behind the account pages (plan AS-031): follows, preferences, digest
consent, deletion and data export. Every function takes the caller's session and leaves the
commit to the caller."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal.models import (
    AppUser,
    DeliveryAttempt,
    DeliveryCorrectionWork,
    Event,
    Feedback,
    Follow,
    LoginToken,
    Notification,
    Outbox,
    Preference,
    Situation,
    UserSession,
)
from africasignal.publish import deletions, tokens
from africasignal.publish.email_render import UNSUBSCRIBE_PURPOSE

REMOVED = "[removed]"


def follow(session: Session, user_id: int, situation_id: int) -> bool:
    """Follow a situation. Returns False when it was already followed."""
    return (
        session.execute(
            pg_insert(Follow)
            .values(user_id=user_id, situation_id=situation_id)
            .on_conflict_do_nothing(index_elements=[Follow.user_id, Follow.situation_id])
            .returning(Follow.id)
        ).scalar_one_or_none()
        is not None
    )


def unfollow(session: Session, user_id: int, situation_id: int) -> bool:
    result = session.execute(
        delete(Follow).where(Follow.user_id == user_id, Follow.situation_id == situation_id)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def followed_situations(session: Session, user_id: int) -> list[Situation]:
    return list(
        session.scalars(
            select(Situation)
            .join(Follow, Follow.situation_id == Situation.id)
            .where(Follow.user_id == user_id)
            .order_by(Follow.created_at, Follow.id)
        )
    )


def set_preferences(
    session: Session, user_id: int, place_ids: list[int], topics: list[str]
) -> None:
    session.execute(
        pg_insert(Preference)
        .values(user_id=user_id, place_ids=place_ids, topics=topics)
        .on_conflict_do_update(
            index_elements=[Preference.user_id],
            set_={"place_ids": place_ids, "topics": topics},
        )
    )


def set_digest_opt_in(session: Session, user_id: int, opted_in: bool, now: datetime) -> None:
    """Consent to (or withdraw from) the weekly digest. Only a verified address can opt in."""
    user = session.get(AppUser, user_id)
    if user is None or user.deleted_at is not None:
        return
    if opted_in and user.email_verified_at is None:
        raise ValueError("the email address is not verified")
    if opted_in and not user.digest_opt_in:
        user.digest_opt_in_at = now
    user.digest_opt_in = opted_in


def set_insight_email_opt_in(session: Session, user_id: int, opted_in: bool, now: datetime) -> None:
    """Manage consent for immediate, reviewed insight emails, separately from the digest."""
    user = session.get(AppUser, user_id)
    if user is None or user.deleted_at is not None:
        return
    if opted_in and user.email_verified_at is None:
        raise ValueError("the email address is not verified")
    if opted_in and not user.insight_email_opt_in:
        user.insight_email_opt_in_at = now
    user.insight_email_opt_in = opted_in
    if not opted_in:
        session.execute(
            update(Outbox)
            .where(
                Outbox.kind == "email_insight",
                Outbox.payload["user_id"].as_integer() == user_id,
                Outbox.status.in_(("pending", "failed")),
            )
            .values(status="dead", last_error="insight email consent withdrawn")
        )


def unsubscribe_token(user_id: int) -> str:
    return tokens.sign(UNSUBSCRIBE_PURPOSE, str(user_id))


def unsubscribe(session: Session, token: str, *, list_name: str = "digest") -> bool:
    """One-click unsubscribe from one mail stream. Repeating a valid link is harmless."""
    purpose = "unsubscribe-insight" if list_name == "insights" else UNSUBSCRIBE_PURPOSE
    if list_name not in {"digest", "insights"}:
        return False
    value = tokens.verify(purpose, token)
    if value is None or not value.isdigit():
        return False
    column = AppUser.insight_email_opt_in if list_name == "insights" else AppUser.digest_opt_in
    session.execute(update(AppUser).where(AppUser.id == int(value)).values({column.key: False}))
    if list_name == "insights":
        session.execute(
            update(Outbox)
            .where(
                Outbox.kind == "email_insight",
                Outbox.payload["user_id"].as_integer() == int(value),
                Outbox.status.in_(("pending", "failed")),
            )
            .values(status="dead", last_error="insight email consent withdrawn")
        )
    return True


def erase_user(session: Session, user: AppUser, *, now: datetime | None = None) -> None:
    """Hard-delete the user and everything tied to them. Feedback is kept but anonymised: it is
    unlinked from the user, the typed contact email is removed, and the address is removed from
    the free text. Delivery receipts are retained with their user linkage redacted. Does not write
    to the deletion ledger."""
    user_id = user.id
    email = user.email
    erased_at = now or datetime.now(UTC)
    session.execute(delete(Notification).where(Notification.user_id == user_id))
    session.execute(delete(Follow).where(Follow.user_id == user_id))
    session.execute(delete(Preference).where(Preference.user_id == user_id))
    session.execute(delete(LoginToken).where(LoginToken.user_id == user_id))
    session.execute(delete(UserSession).where(UserSession.user_id == user_id))
    outbox_ids = session.scalars(
        select(Outbox.id).where(Outbox.payload["user_id"].as_integer() == user_id)
    ).all()
    attempt_predicates = [DeliveryAttempt.target_reference == f"app_user:{user_id}"]
    if outbox_ids:
        attempt_predicates.append(DeliveryAttempt.outbox_id.in_(outbox_ids))
    attempts = session.scalars(select(DeliveryAttempt).where(or_(*attempt_predicates))).all()
    attempt_ids = [attempt.id for attempt in attempts]
    if attempt_ids:
        correction_predicate = or_(
            DeliveryCorrectionWork.delivery_attempt_id.in_(attempt_ids),
            DeliveryCorrectionWork.follow_up_delivery_attempt_id.in_(attempt_ids),
        )
        correction_work = session.scalars(
            select(DeliveryCorrectionWork).where(correction_predicate)
        ).all()
        for work in correction_work:
            work.reason_detail = re.sub(
                re.escape(email), REMOVED, work.reason_detail, flags=re.IGNORECASE
            )
            if work.operator_note:
                work.operator_note = re.sub(
                    re.escape(email), REMOVED, work.operator_note, flags=re.IGNORECASE
                )
            if work.state == "pending_review":
                work.state = "blocked"
                work.operator_note = "Recipient account erased; correction delivery is prohibited."
        for attempt in attempts:
            attempt.outbox_id = None
            attempt.owner_erased_at = erased_at
            attempt.destination_sha256 = None
            attempt.target_reference = None
            attempt.assessment_version_ids = []
            attempt.authorization_snapshot = {"kind": "privacy_erased"}
            if attempt.outcome_detail:
                attempt.outcome_detail = re.sub(
                    re.escape(email), REMOVED, attempt.outcome_detail, flags=re.IGNORECASE
                )
    if outbox_ids:
        session.execute(delete(Outbox).where(Outbox.id.in_(outbox_ids)))
    session.execute(update(Event).where(Event.user_id == user_id).values(user_id=None))
    session.execute(update(Feedback).where(Feedback.user_id == user_id).values(user_id=None))
    session.execute(
        update(Feedback)
        .where(func.lower(Feedback.contact_email) == email.lower())
        .values(contact_email=None)
    )
    session.execute(
        update(Feedback)
        .where(Feedback.text.ilike(f"%{email}%"))
        .values(text=func.regexp_replace(Feedback.text, re.escape(email), REMOVED, "gi"))
    )
    session.delete(user)
    session.flush()


def delete_account(session: Session, user_id: int, now: datetime | None = None) -> bool:
    """The account holder's own deletion: erase the user (see ``erase_user``) and note it in the
    deletion ledger, so a restored backup can delete the account again. Returns False when the
    user does not exist."""
    user = session.get(AppUser, user_id)
    if user is None:
        return False
    deleted_at = now or datetime.now(UTC)
    deletions.record(session, user.email, deleted_at)
    erase_user(session, user, now=deleted_at)
    return True


def export_account(session: Session, user_id: int) -> dict[str, Any] | None:
    """Everything held about a user, as JSON-serialisable data (the "export my data" link)."""
    user = session.get(AppUser, user_id)
    if user is None:
        return None
    preference = session.get(Preference, user_id)
    follows = session.execute(
        select(Situation.slug, Follow.created_at)
        .join(Follow, Follow.situation_id == Situation.id)
        .where(Follow.user_id == user_id)
        .order_by(Follow.created_at)
    ).all()
    notifications = session.scalars(
        select(Notification).where(Notification.user_id == user_id).order_by(Notification.id)
    ).all()
    feedback = session.scalars(
        select(Feedback).where(Feedback.user_id == user_id).order_by(Feedback.id)
    ).all()

    events = session.execute(
        select(Event.ts, Event.name, Situation.slug, Event.ref, Event.anon_id, Event.props)
        .outerjoin(Situation, Situation.id == Event.situation_id)
        .where(Event.user_id == user_id)
        .order_by(Event.ts, Event.id)
    ).all()

    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "email": user.email,
        "created_at": iso(user.created_at),
        "email_verified_at": iso(user.email_verified_at),
        "digest_opt_in": user.digest_opt_in,
        "digest_opt_in_at": iso(user.digest_opt_in_at),
        "insight_email_opt_in": user.insight_email_opt_in,
        "insight_email_opt_in_at": iso(user.insight_email_opt_in_at),
        "preferences": {
            "place_ids": list(preference.place_ids) if preference else [],
            "topics": list(preference.topics) if preference else [],
        },
        "follows": [{"situation": slug, "since": iso(since)} for slug, since in follows],
        "notifications": [
            {
                "kind": n.kind,
                "assessment_version_id": n.assessment_version_id,
                "created_at": iso(n.created_at),
                "read_at": iso(n.read_at),
                "cancelled_at": iso(n.cancelled_at),
            }
            for n in notifications
        ],
        "feedback": [
            {
                "kind": f.kind,
                "text": f.text,
                "contact_email": f.contact_email,
                "visitor_code": f.anon_id,
                "created_at": iso(f.created_at),
            }
            for f in feedback
        ],
        # Page views and other events recorded while signed in (they carry the account number).
        # Each also carries the random visitor code of the browser it came from.
        "events": [
            {
                "time": iso(ts),
                "event": name,
                "situation": slug,
                "came_from": ref,
                "details": props,
                "visitor_code": anon_id,
            }
            for ts, name, slug, ref, anon_id, props in events
        ],
    }
