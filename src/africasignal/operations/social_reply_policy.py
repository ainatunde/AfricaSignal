"""Fail-closed rules for the versioned X reply-policy draft."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.config import get_settings
from africasignal.models import Operator, SocialReplyPolicy
from africasignal.operations.assessments import AssessmentError, clean_reason
from africasignal.publish.suspension import publication_suspended

ALLOWED_CATEGORIES = frozenset({"dated_fact", "source_request"})
ESCALATION_CATEGORIES = frozenset(
    {
        "clarification",
        "correction_report",
        "unsupported_premise",
        "out_of_scope",
        "abuse_or_spam",
        "other",
    }
)
REQUIRED_EVALUATION_CASE_FAMILIES = frozenset(
    {
        "ambiguous_scope_or_time",
        "contradictory_evidence",
        "stale_evidence",
        "correction_report",
        "malicious_instructions",
        "repeated_mentions",
        "bot_loop",
    }
)
REQUIRED_EVALUATION_CASE_SET = "africasignal-x-replies-v1"

DEFAULT_POLICY: dict[str, Any] = {
    "channel": "x",
    "country_code": "NG",
    "allowed_topics": ["energy", "food"],
    "allowed_categories": ["dated_fact", "source_request"],
    "escalate_categories": sorted(ESCALATION_CATEGORIES),
    "max_replies_per_lagos_day": 10,
    "max_replies_per_conversation": 1,
    "requires_operator_triage": True,
    "requires_assessment_selection": True,
    "requires_individual_reply_review": True,
    "stores_mention_text": False,
}
POLICY_KEYS = frozenset(DEFAULT_POLICY)


def validate_policy(policy: object) -> bool:
    """Reject changed or malformed policy bounds instead of interpreting them loosely."""
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        return False
    allowed_topics = policy.get("allowed_topics")
    categories = policy.get("allowed_categories")
    escalations = policy.get("escalate_categories")
    daily_cap = policy.get("max_replies_per_lagos_day")
    conversation_cap = policy.get("max_replies_per_conversation")
    return bool(
        policy.get("channel") == "x"
        and policy.get("country_code") == "NG"
        and isinstance(allowed_topics, list)
        and allowed_topics
        and all(isinstance(value, str) for value in allowed_topics)
        and set(allowed_topics) <= {"energy", "food"}
        and isinstance(categories, list)
        and all(isinstance(value, str) for value in categories)
        and set(categories) == ALLOWED_CATEGORIES
        and isinstance(escalations, list)
        and all(isinstance(value, str) for value in escalations)
        and set(escalations) == ESCALATION_CATEGORIES
        and isinstance(daily_cap, int)
        and not isinstance(daily_cap, bool)
        and 1 <= daily_cap <= 10
        and isinstance(conversation_cap, int)
        and not isinstance(conversation_cap, bool)
        and conversation_cap == 1
        and policy.get("requires_operator_triage") is True
        and policy.get("requires_assessment_selection") is True
        and policy.get("requires_individual_reply_review") is True
        and policy.get("stores_mention_text") is False
    )


def policy_allows(
    policy: object,
    *,
    category: str | None,
    topic: str,
    country_code: str,
    conversation_id_present: bool,
    replies_today: int,
    replies_in_conversation: int,
) -> bool:
    """Return whether a human-triaged request fits the configured reply bounds."""
    if not validate_policy(policy):
        return False
    assert isinstance(policy, dict)
    return bool(
        category in policy["allowed_categories"]
        and topic in policy["allowed_topics"]
        and country_code == policy["country_code"]
        and conversation_id_present
        and 0 <= replies_today < policy["max_replies_per_lagos_day"]
        and 0 <= replies_in_conversation < policy["max_replies_per_conversation"]
    )


def evaluation_is_complete(case_count: int, coverage: object, critical_errors: int) -> bool:
    """Require passing recorded coverage for each safety family before policy approval."""
    if (
        isinstance(case_count, bool)
        or not isinstance(case_count, int)
        or case_count < len(REQUIRED_EVALUATION_CASE_FAMILIES)
        or isinstance(critical_errors, bool)
        or not isinstance(critical_errors, int)
        or critical_errors != 0
        or not isinstance(coverage, dict)
        or set(coverage) != REQUIRED_EVALUATION_CASE_FAMILIES
    ):
        return False
    total = 0
    for result in coverage.values():
        if not isinstance(result, dict):
            return False
        cases = result.get("cases")
        passed = result.get("passed")
        if (
            isinstance(cases, bool)
            or not isinstance(cases, int)
            or not 1 <= cases <= 500
            or isinstance(passed, bool)
            or not isinstance(passed, int)
            or passed != cases
        ):
            return False
        total += cases
    return total == case_count


def evaluation_is_recordable(case_count: int, coverage: object) -> bool:
    """Check that a submitted report is structurally complete, whether it passed or failed."""
    if (
        isinstance(case_count, bool)
        or not isinstance(case_count, int)
        or case_count < len(REQUIRED_EVALUATION_CASE_FAMILIES)
        or not isinstance(coverage, dict)
        or set(coverage) != REQUIRED_EVALUATION_CASE_FAMILIES
    ):
        return False
    total = 0
    for result in coverage.values():
        if not isinstance(result, dict):
            return False
        cases = result.get("cases")
        passed = result.get("passed")
        if (
            isinstance(cases, bool)
            or not isinstance(cases, int)
            or not 1 <= cases <= 500
            or isinstance(passed, bool)
            or not isinstance(passed, int)
            or not 0 <= passed <= cases
        ):
            return False
        total += cases
    return total == case_count


def record_evaluation(
    session: Session,
    operator: Operator,
    revision: int,
    coverage: object,
    critical_errors: int,
    evidence_ref: str,
    *,
    now: datetime | None = None,
) -> SocialReplyPolicy:
    """Record an externally reviewed conversation evaluation with complete case-family coverage."""
    ref = _clean_evidence_ref(evidence_ref)
    if (
        not isinstance(coverage, dict)
        or isinstance(critical_errors, bool)
        or not isinstance(critical_errors, int)
    ):
        raise ValueError("provide structured evaluation coverage and a critical-error count")
    count = sum(
        int(result.get("cases", 0))
        for result in coverage.values()
        if isinstance(result, dict) and isinstance(result.get("cases"), int)
    )
    if critical_errors < 0 or not evaluation_is_recordable(count, coverage):
        raise ValueError("reply evaluation report is incomplete or malformed")
    row = session.scalar(
        select(SocialReplyPolicy).where(SocialReplyPolicy.revision == revision).with_for_update()
    )
    if row is None or row.status != "draft" or not validate_policy(row.policy):
        raise ValueError("only a valid draft policy can be evaluated")
    now = now or datetime.now(UTC)
    before = row.status
    passed = evaluation_is_complete(count, coverage, critical_errors)
    row.status = "evaluated" if passed else "draft"
    row.evaluation_case_set = REQUIRED_EVALUATION_CASE_SET
    row.evaluation_evidence_ref = ref
    row.evaluation_case_count = count
    row.evaluation_coverage = coverage
    row.evaluation_critical_errors = critical_errors
    row.evaluated_by_operator_id = operator.id
    row.evaluated_at = now
    session.flush()
    audit.record(
        session,
        operator,
        "social_reply_policy.evaluated" if passed else "social_reply_policy.evaluation_failed",
        "social_reply_policy",
        row.id,
        before={"status": before},
        after={
            "status": row.status,
            "revision": row.revision,
            "case_set": row.evaluation_case_set,
            "case_count": count,
            "coverage": coverage,
            "critical_errors": critical_errors,
            "passed": passed,
            "evidence_ref": ref,
        },
    )
    return row


def approve(
    session: Session,
    operator: Operator,
    revision: int,
    reason: str,
    *,
    now: datetime | None = None,
) -> SocialReplyPolicy:
    """Record editorial approval after a clean, complete evaluation."""
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise ValueError(str(exc)) from None
    row = session.scalar(
        select(SocialReplyPolicy).where(SocialReplyPolicy.revision == revision).with_for_update()
    )
    if (
        row is None
        or row.status != "evaluated"
        or row.evaluated_by_operator_id == operator.id
        or not validate_policy(row.policy)
        or row.evaluation_case_set != REQUIRED_EVALUATION_CASE_SET
        or row.evaluation_case_count is None
        or row.evaluation_critical_errors is None
        or not evaluation_is_complete(
            row.evaluation_case_count, row.evaluation_coverage, row.evaluation_critical_errors
        )
        or not (row.evaluation_evidence_ref or "").strip()
    ):
        raise ValueError("a different editor must approve a complete, clean evaluation")
    now = now or datetime.now(UTC)
    before = row.status
    row.status = "approved"
    row.approved_by_operator_id = operator.id
    row.approved_at = now
    row.approval_reason = reason
    session.flush()
    audit.record(
        session,
        operator,
        "social_reply_policy.approved",
        "social_reply_policy",
        row.id,
        before={"status": before},
        after={"status": row.status, "revision": row.revision, "reason": reason},
    )
    return row


def activate(
    session: Session,
    operator: Operator,
    revision: int,
    staging_evidence_ref: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> SocialReplyPolicy:
    """Activate only after evaluation, separate editorial approval, and staged evidence."""
    staging_ref = _clean_evidence_ref(staging_evidence_ref)
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise ValueError(str(exc)) from None
    row = session.scalar(
        select(SocialReplyPolicy).where(SocialReplyPolicy.revision == revision).with_for_update()
    )
    if (
        row is None
        or row.status != "approved"
        or row.approved_by_operator_id == operator.id
        or not validate_policy(row.policy)
        or row.evaluation_case_set != REQUIRED_EVALUATION_CASE_SET
        or row.evaluation_case_count is None
        or row.evaluation_critical_errors is None
        or not evaluation_is_complete(
            row.evaluation_case_count, row.evaluation_coverage, row.evaluation_critical_errors
        )
        or not (row.evaluation_evidence_ref or "").strip()
        or not (row.approval_reason or "").strip()
    ):
        raise ValueError("policy activation requires a complete evaluation and editorial approval")
    account_id = settings_store.get(session, "x_account_user_id") or ""
    if (
        not account_id.isdigit()
        or len(account_id) > 19
        or not settings_store.get(session, "x_user_access_token")
        or settings_store.get(session, "x_replies_enabled") != "yes"
        or settings_store.get(session, "x_publishing_enabled") != "yes"
        or publication_suspended(session)
    ):
        raise ValueError("the X account, reply controls, or publication state is not ready")
    if get_settings().env != "staging":
        raise ValueError("reply-policy activation is limited to a qualified staging environment")
    active = session.scalar(
        select(SocialReplyPolicy).where(SocialReplyPolicy.status == "active").with_for_update()
    )
    if active is not None:
        raise ValueError("retire the current reply policy before activating a new revision")
    now = now or datetime.now(UTC)
    before = row.status
    row.status = "active"
    row.account_user_id = account_id
    row.staging_evidence_ref = staging_ref
    row.activated_by_operator_id = operator.id
    row.activated_at = now
    row.activation_reason = reason
    session.flush()
    audit.record(
        session,
        operator,
        "social_reply_policy.activated",
        "social_reply_policy",
        row.id,
        before={"status": before},
        after={
            "status": row.status,
            "revision": row.revision,
            "account_user_id": account_id,
            "staging_evidence_ref": staging_ref,
            "reason": reason,
        },
    )
    return row


def retire(
    session: Session,
    operator: Operator,
    revision: int,
    reason: str,
    *,
    now: datetime | None = None,
) -> SocialReplyPolicy:
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise ValueError(str(exc)) from None
    row = session.scalar(
        select(SocialReplyPolicy).where(SocialReplyPolicy.revision == revision).with_for_update()
    )
    if row is None or row.status != "active":
        raise ValueError("only the active policy can be retired")
    now = now or datetime.now(UTC)
    row.status = "retired"
    session.flush()
    audit.record(
        session,
        operator,
        "social_reply_policy.retired",
        "social_reply_policy",
        row.id,
        before={"status": "active", "revision": revision},
        after={
            "status": "retired",
            "revision": revision,
            "reason": reason,
            "retired_at": now.isoformat(),
        },
    )
    return row


def revise(
    session: Session,
    operator: Operator,
    revision: int,
    reason: str,
    policy: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> SocialReplyPolicy:
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise ValueError(str(exc)) from None
    source = session.scalar(select(SocialReplyPolicy).where(SocialReplyPolicy.revision == revision))
    if source is None or source.status != "retired":
        raise ValueError("retire the active revision before creating its next draft")
    next_policy = dict(policy if policy is not None else source.policy)
    if not validate_policy(next_policy):
        raise ValueError("new policy values exceed the supported reply bounds")
    now = now or datetime.now(UTC)
    latest_revision = session.scalar(select(func.max(SocialReplyPolicy.revision))) or 0
    row = SocialReplyPolicy(
        revision=int(latest_revision) + 1,
        status="draft",
        policy=next_policy,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "social_reply_policy.revised",
        "social_reply_policy",
        row.id,
        after={
            "status": "draft",
            "revision": row.revision,
            "based_on_revision": source.revision,
            "reason": reason,
            "created_at": now.isoformat(),
        },
    )
    return row


def _clean_evidence_ref(value: str) -> str:
    ref = value.strip() if isinstance(value, str) else ""
    if len(ref) < 8 or len(ref) > 300 or any(ord(char) < 32 for char in ref):
        raise ValueError("provide an auditable evaluation/staging evidence reference")
    return ref
