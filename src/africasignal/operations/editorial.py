"""Operator review for private, model-generated insight drafts."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.assess.retrieval import (
    RetrievedPassage,
    assessment_evidence_is_current,
    assessment_passages,
)
from africasignal.models import (
    AssessmentVersion,
    EditorialInsightDraft,
    Operator,
    Situation,
)
from africasignal.operations.assessments import AssessmentError, clean_reason
from africasignal.publish.notify import queue_reviewed_insight_email


class EditorialReviewError(ValueError):
    """A draft is stale or not ready for the requested review action."""


def recent(
    session: Session, limit: int = 50
) -> list[tuple[EditorialInsightDraft, AssessmentVersion, Situation]]:
    rows = session.execute(
        select(EditorialInsightDraft, AssessmentVersion, Situation)
        .join(
            AssessmentVersion,
            AssessmentVersion.id == EditorialInsightDraft.assessment_version_id,
        )
        .join(Situation, Situation.id == AssessmentVersion.situation_id)
        .order_by(EditorialInsightDraft.created_at.desc(), EditorialInsightDraft.id.desc())
        .limit(limit)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def evidence_for(session: Session, draft: EditorialInsightDraft) -> list[RetrievedPassage]:
    return assessment_passages(session, draft.assessment_version_id)


def review(
    session: Session,
    operator: Operator,
    draft_id: int,
    decision: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> EditorialInsightDraft:
    if decision not in {"approved", "email_queued", "rejected"}:
        raise EditorialReviewError("choose approve, approve and email, or reject")
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise EditorialReviewError(str(exc)) from None
    now = now or datetime.now(UTC)
    draft = session.scalar(
        select(EditorialInsightDraft).where(EditorialInsightDraft.id == draft_id).with_for_update()
    )
    if draft is None:
        raise EditorialReviewError("no such editorial draft")
    if draft.status != "pending_review" and not (
        draft.status == "approved" and decision == "email_queued"
    ):
        raise EditorialReviewError("that editorial draft is no longer awaiting review or delivery")
    if draft.status == "approved" and decision != "email_queued":
        raise EditorialReviewError("an internally approved draft can only be queued for email")
    version = session.get(AssessmentVersion, draft.assessment_version_id)
    situation = session.get(Situation, version.situation_id) if version else None
    if version is None or situation is None:
        raise EditorialReviewError("the source assessment no longer exists")
    before = draft.status
    if decision in {"approved", "email_queued"}:
        if (
            version.status != "published"
            or situation.current_version_id != version.id
            or (version.valid_until is not None and version.valid_until <= now)
        ):
            draft.status = "stale"
            draft.review_note = "The assessment is no longer current or within its validity window."
            draft.reviewed_by_operator_id = operator.id
            draft.reviewed_at = now
            session.flush()
            audit.record(
                session,
                operator,
                "editorial_draft.marked_stale",
                "editorial_insight_draft",
                draft.id,
                before={"status": before, "assessment_version_id": version.id},
                after={"status": draft.status, "reason": draft.review_note},
            )
            raise EditorialReviewError(
                "the assessment changed or expired; this draft was marked stale"
            )
        current_claim_ids = {item.claim_id for item in assessment_passages(session, version.id)}
        if not set(draft.evidence_claim_ids).issubset(current_claim_ids):
            draft.status = "stale"
            draft.review_note = "A source permission, claim, or retention condition changed."
            draft.reviewed_by_operator_id = operator.id
            draft.reviewed_at = now
            session.flush()
            audit.record(
                session,
                operator,
                "editorial_draft.marked_stale",
                "editorial_insight_draft",
                draft.id,
                before={"status": before, "assessment_version_id": version.id},
                after={"status": draft.status, "reason": draft.review_note},
            )
            raise EditorialReviewError("the source context changed; this draft was marked stale")
        if not assessment_evidence_is_current(session, version.id, now=now):
            draft.status = "stale"
            draft.review_note = (
                "One or more assessment evidence documents expired or were withdrawn."
            )
            draft.reviewed_by_operator_id = operator.id
            draft.reviewed_at = now
            session.flush()
            audit.record(
                session,
                operator,
                "editorial_draft.marked_stale",
                "editorial_insight_draft",
                draft.id,
                before={"status": before, "assessment_version_id": version.id},
                after={"status": draft.status, "reason": draft.review_note},
            )
            raise EditorialReviewError("assessment evidence changed; this draft was marked stale")
    draft.status = decision
    email_count = 0
    if decision == "email_queued":
        try:
            email_count = queue_reviewed_insight_email(session, draft, now)
        except ValueError as exc:
            draft.status = before
            raise EditorialReviewError(str(exc)) from None
        if email_count == 0:
            draft.status = before
            raise EditorialReviewError(
                "no verified followers have opted in to insight emails; no email was queued"
            )
    draft.reviewed_by_operator_id = operator.id
    draft.reviewed_at = now
    draft.review_note = reason
    session.flush()
    audit.record(
        session,
        operator,
        "editorial_draft.approved_for_email"
        if decision == "email_queued"
        else f"editorial_draft.{decision}",
        "editorial_insight_draft",
        draft.id,
        before={"status": before, "assessment_version_id": version.id},
        after={"status": decision, "reason": reason, "emails_queued": email_count},
    )
    return draft
