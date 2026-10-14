"""Admin review operations for deterministic insight candidates."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.assess.insight_candidates import mark_stale_candidates
from africasignal.models import (
    AssessmentVersion,
    InsightCandidate,
    InsightCandidateAssessment,
    Operator,
    Situation,
)
from africasignal.operations.assessments import AssessmentError, clean_reason
from africasignal.publish.eligibility import publication_eligibility_many


class InsightCandidateError(ValueError):
    """A candidate cannot be reviewed against the current evidence and permissions."""


def recent(
    session: Session, limit: int = 50, *, now: datetime | None = None
) -> list[InsightCandidate]:
    mark_stale_candidates(session, now or datetime.now(UTC))
    return list(
        session.scalars(
            select(InsightCandidate)
            .order_by(InsightCandidate.created_at.desc(), InsightCandidate.id.desc())
            .limit(limit)
        )
    )


def review(
    session: Session,
    operator: Operator,
    candidate_id: int,
    decision: str,
    reason: str,
    usefulness_rating: int,
    scope_correct: bool,
    unsupported_critical: bool,
    *,
    now: datetime | None = None,
) -> InsightCandidate:
    if decision not in {"selected", "rejected"}:
        raise InsightCandidateError("choose select or reject")
    if (
        isinstance(usefulness_rating, bool)
        or not isinstance(usefulness_rating, int)
        or not 1 <= usefulness_rating <= 5
        or type(scope_correct) is not bool
        or type(unsupported_critical) is not bool
    ):
        raise InsightCandidateError(
            "rate usefulness from 1 to 5 and assess scope and critical support"
        )
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise InsightCandidateError(str(exc)) from None
    now = now or datetime.now(UTC)
    candidate = session.scalar(
        select(InsightCandidate).where(InsightCandidate.id == candidate_id).with_for_update()
    )
    if candidate is None:
        raise InsightCandidateError("no such insight candidate")
    if candidate.status != "pending_review":
        raise InsightCandidateError("that insight candidate is no longer awaiting review")

    members = session.execute(
        select(InsightCandidateAssessment.role, AssessmentVersion)
        .select_from(InsightCandidateAssessment)
        .join(
            AssessmentVersion,
            AssessmentVersion.id == InsightCandidateAssessment.assessment_version_id,
        )
        .where(InsightCandidateAssessment.candidate_id == candidate.id)
        .order_by(InsightCandidateAssessment.ordinal, AssessmentVersion.id)
    ).all()
    member_rows = [(role, version) for role, version in members]
    versions = [version for _role, version in member_rows]
    decisions = publication_eligibility_many(
        session, versions, channel="website", now=now, require_current=False
    )
    situations = {
        situation.id: situation
        for situation in session.scalars(
            select(Situation).where(
                Situation.id.in_({version.situation_id for version in versions})
            )
        )
    }
    current_versions = {
        version.situation_id: version for role, version in member_rows if role != "prior"
    }
    current = bool(member_rows) and all(
        decisions.get(version.id) is not None
        and decisions[version.id].eligible
        and (version.valid_until is None or version.valid_until > now)
        and (
            version.status == "superseded"
            and current_versions.get(version.situation_id) is not None
            and current_versions[version.situation_id].supersedes_id == version.id
            if role == "prior"
            else version.status == "published"
            and situations.get(version.situation_id) is not None
            and situations[version.situation_id].current_version_id == version.id
        )
        for role, version in member_rows
    )
    if not current:
        candidate.status = "stale"
        candidate.stale_reason = "Assessment eligibility or current evidence changed."
        candidate.reviewed_by_operator_id = operator.id
        candidate.reviewed_at = now
        session.flush()
        audit.record(
            session,
            operator,
            "insight_candidate.marked_stale",
            "insight_candidate",
            candidate.id,
            before={"status": "pending_review"},
            after={"status": "stale", "reason": candidate.stale_reason},
        )
        raise InsightCandidateError("the evidence packet changed; this candidate was marked stale")

    candidate.status = decision
    candidate.reviewed_by_operator_id = operator.id
    candidate.reviewed_at = now
    candidate.review_note = reason
    candidate.usefulness_rating = usefulness_rating
    candidate.scope_correct = scope_correct
    candidate.unsupported_critical = unsupported_critical
    session.flush()
    audit.record(
        session,
        operator,
        f"insight_candidate.{decision}",
        "insight_candidate",
        candidate.id,
        before={"status": "pending_review"},
        after={
            "status": decision,
            "assessment_version_ids": [version.id for version in versions],
            "reason": reason,
            "evaluation": {
                "usefulness_rating": usefulness_rating,
                "scope_correct": scope_correct,
                "unsupported_critical": unsupported_critical,
            },
            "publication_authorized": False,
        },
    )
    return candidate


def quality_metrics(session: Session) -> dict[str, int | float | None]:
    """Summarize human usefulness/scope labels; the result is not a held-out evaluation."""
    total = int(
        session.scalar(
            select(func.count())
            .select_from(InsightCandidate)
            .where(
                InsightCandidate.usefulness_rating.is_not(None),
                InsightCandidate.scope_correct.is_not(None),
                InsightCandidate.unsupported_critical.is_not(None),
            )
        )
        or 0
    )
    successful = int(
        session.scalar(
            select(func.count())
            .select_from(InsightCandidate)
            .where(
                InsightCandidate.usefulness_rating >= 4,
                InsightCandidate.scope_correct.is_(True),
                InsightCandidate.unsupported_critical.is_not(None),
            )
        )
        or 0
    )
    critical = int(
        session.scalar(
            select(func.count())
            .select_from(InsightCandidate)
            .where(InsightCandidate.unsupported_critical.is_(True))
        )
        or 0
    )
    return {
        "reviewed": total,
        "useful_and_scoped": successful,
        "useful_and_scoped_pct": round(100 * successful / total, 1) if total else None,
        "unsupported_critical": critical,
    }
