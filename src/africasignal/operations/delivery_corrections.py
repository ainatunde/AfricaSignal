"""Review work for externally accepted content later affected by invalidation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import (
    AssessmentVersion,
    DeliveryAttempt,
    DeliveryCorrectionWork,
    Operator,
    Situation,
)
from africasignal.publish.eligibility import publication_eligibility


class DeliveryCorrectionError(ValueError):
    """A correction work item cannot be safely resolved."""


@dataclass(frozen=True)
class CorrectionView:
    work: DeliveryCorrectionWork
    original: DeliveryAttempt
    invalidated_version: AssessmentVersion
    situation: Situation
    current_version: AssessmentVersion | None
    follow_ups: list[DeliveryAttempt]


def record_for_version(
    session: Session,
    assessment_version_id: int,
    *,
    reason_key: str,
    reason_code: str,
    reason_detail: str,
    now: datetime,
) -> int:
    """Create one idempotent work item for every confirmed external delivery of this version."""
    attempts = session.scalars(
        select(DeliveryAttempt).where(
            DeliveryAttempt.status == "sent",
            DeliveryAttempt.assessment_version_ids.contains([assessment_version_id]),
        )
    )
    created = 0
    for attempt in attempts:
        inserted = session.execute(
            pg_insert(DeliveryCorrectionWork)
            .values(
                delivery_attempt_id=attempt.id,
                invalidated_assessment_version_id=assessment_version_id,
                reason_key=reason_key,
                reason_code=reason_code,
                reason_detail=reason_detail[:1000],
                state="pending_review",
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    DeliveryCorrectionWork.delivery_attempt_id,
                    DeliveryCorrectionWork.reason_key,
                ]
            )
            .returning(DeliveryCorrectionWork.id)
        ).scalar_one_or_none()
        created += inserted is not None
    return created


def _follow_up_candidates(
    session: Session,
    original: DeliveryAttempt,
    work_created_at: datetime,
    invalidated: AssessmentVersion,
    current: AssessmentVersion | None,
    now: datetime,
) -> list[DeliveryAttempt]:
    if (
        current is None
        or current.id == invalidated.id
        or current.status != "published"
        or current.situation_id != invalidated.situation_id
    ):
        return []
    if (
        session.scalar(
            select(Situation.id).where(
                Situation.id == current.situation_id,
                Situation.current_version_id == current.id,
            )
        )
        is None
    ):
        return []
    eligibility_channel: Literal["email", "social", "reply"] = (
        "email"
        if original.channel == "email"
        else "reply"
        if original.social_reply_id is not None
        else "social"
    )
    if not publication_eligibility(session, current, channel=eligibility_channel, now=now).eligible:
        return []
    candidates = session.scalars(
        select(DeliveryAttempt).where(
            DeliveryAttempt.status == "sent",
            DeliveryAttempt.channel == original.channel,
            DeliveryAttempt.assessment_version_ids.contains([current.id]),
            DeliveryAttempt.attempted_at >= work_created_at,
        )
    )
    result: list[DeliveryAttempt] = []
    for attempt in candidates:
        if attempt.id == original.id:
            continue
        if attempt.social_reply_id is not None:
            if (
                original.social_reply_id is None
                or attempt.target_reference != original.target_reference
            ):
                continue
        elif original.social_reply_id is not None:
            continue
        if original.channel == "email":
            if attempt.destination_sha256 != original.destination_sha256:
                continue
        elif attempt.destination_account_id != original.destination_account_id:
            continue
        result.append(attempt)
    return result


def listing(session: Session, *, now: datetime | None = None) -> list[CorrectionView]:
    now = now or datetime.now(UTC)
    works = session.scalars(
        select(DeliveryCorrectionWork)
        .order_by(DeliveryCorrectionWork.created_at.desc(), DeliveryCorrectionWork.id.desc())
        .limit(100)
    )
    views: list[CorrectionView] = []
    for work in works:
        original = session.get(DeliveryAttempt, work.delivery_attempt_id)
        invalidated = session.get(AssessmentVersion, work.invalidated_assessment_version_id)
        if original is None or invalidated is None:
            continue
        situation = session.get(Situation, invalidated.situation_id)
        if situation is None:
            continue
        current = (
            session.get(AssessmentVersion, situation.current_version_id)
            if situation.current_version_id is not None
            else None
        )
        views.append(
            CorrectionView(
                work=work,
                original=original,
                invalidated_version=invalidated,
                situation=situation,
                current_version=current,
                follow_ups=_follow_up_candidates(
                    session, original, work.created_at, invalidated, current, now
                ),
            )
        )
    return views


def review(
    session: Session,
    operator: Operator,
    work_id: int,
    decision: str,
    reason: str,
    *,
    follow_up_attempt_id: int | None = None,
    now: datetime | None = None,
) -> DeliveryCorrectionWork:
    from africasignal.operations.assessments import AssessmentError, clean_reason

    if decision not in {"fulfilled", "blocked", "dismissed"}:
        raise DeliveryCorrectionError("choose fulfilled, blocked, or dismissed")
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise DeliveryCorrectionError(str(exc)) from None
    now = now or datetime.now(UTC)
    work = session.scalar(
        select(DeliveryCorrectionWork).where(DeliveryCorrectionWork.id == work_id).with_for_update()
    )
    if work is None or work.state != "pending_review":
        raise DeliveryCorrectionError("that correction work item is not pending review")

    follow_up: DeliveryAttempt | None = None
    replacement: AssessmentVersion | None = None
    if decision == "fulfilled":
        if follow_up_attempt_id is None:
            raise DeliveryCorrectionError("select a confirmed follow-up delivery receipt")
        view = next((row for row in listing(session, now=now) if row.work.id == work.id), None)
        if view is None:
            raise DeliveryCorrectionError("the source delivery or current assessment is missing")
        follow_up = next((row for row in view.follow_ups if row.id == follow_up_attempt_id), None)
        if follow_up is None or view.current_version is None:
            raise DeliveryCorrectionError(
                "the follow-up must be a confirmed send to the same recipient or account "
                "for the current eligible assessment"
            )
        replacement = view.current_version

    before = {"state": work.state}
    work.state = decision
    work.reviewed_by_operator_id = operator.id
    work.reviewed_at = now
    work.operator_note = reason
    if follow_up is not None and replacement is not None:
        work.follow_up_delivery_attempt_id = follow_up.id
        work.replacement_assessment_version_id = replacement.id
    session.flush()
    audit.record(
        session,
        operator,
        f"delivery_correction.{decision}",
        "delivery_correction_work",
        work.id,
        before=before,
        after={
            "state": work.state,
            "reason": reason,
            "delivery_attempt_id": work.delivery_attempt_id,
            "follow_up_delivery_attempt_id": work.follow_up_delivery_attempt_id,
            "replacement_assessment_version_id": work.replacement_assessment_version_id,
        },
    )
    return work
