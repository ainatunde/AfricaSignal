"""ID-only X mention intake and reviewed, evidence-grounded reply delivery."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.models import (
    AssessmentVersion,
    DeliveryAttempt,
    Operator,
    Place,
    Situation,
    SocialMention,
    SocialMentionPoll,
    SocialReply,
    SocialReplyPolicy,
)
from africasignal.operations import social_reply_policy as reply_policy_ops
from africasignal.operations.assessments import AssessmentError, clean_reason
from africasignal.publish.eligibility import (
    publication_eligibility,
    publication_eligibility_many,
)
from africasignal.publish.suspension import publication_suspended
from africasignal.publish.whatsapp_text import PostError, write_post
from africasignal.publish.x_api import (
    XPostOutcomeUnknown,
    XPostRejected,
    authenticated_user_id,
    create_reply,
    get_mentions,
    post_is_available,
)

log = logging.getLogger("africasignal.social_replies")
LAGOS = ZoneInfo("Africa/Lagos")
MENTION_RETENTION = timedelta(days=30)
POLL_TIMEOUT = timedelta(minutes=10)
POLL_MAX_RESULTS = 20
MAX_DISPATCH_BATCH = 3
MAX_CONVERSATION_REPLIES = 1


class SocialReplyError(ValueError):
    """A mention or reply is no longer safe to process."""


@dataclass
class MentionPollResult:
    fetched: int = 0
    new_mentions: int = 0
    failed: int = 0
    unknown: int = 0
    deferred: int = 0


@dataclass
class ReplyDispatchResult:
    sent: int = 0
    rejected: int = 0
    unknown: int = 0
    cancelled: int = 0
    held: int = 0


def _account_id(session: Session) -> str:
    value = settings_store.get(session, "x_account_user_id") or ""
    if not value.isdigit() or len(value) > 19:
        raise SocialReplyError("the numeric X publishing account user ID is not configured")
    return value


def _token(session: Session) -> str:
    value = settings_store.get(session, "x_user_access_token")
    if not value:
        raise SocialReplyError("the X user access token is not configured")
    return value


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _lagos_day(now: datetime) -> tuple[datetime, datetime]:
    local_day = now.astimezone(LAGOS).date()
    return (
        datetime.combine(local_day, time.min, tzinfo=LAGOS).astimezone(UTC),
        datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=LAGOS).astimezone(UTC),
    )


def _read_usage(session: Session, start: datetime, end: datetime) -> int:
    completed = (
        session.scalar(
            select(func.coalesce(func.sum(SocialMentionPoll.fetched_results), 0)).where(
                SocialMentionPoll.started_at >= start,
                SocialMentionPoll.started_at < end,
            )
        )
        or 0
    )
    reserved = (
        session.scalar(
            select(func.coalesce(func.sum(SocialMentionPoll.reserved_results), 0)).where(
                SocialMentionPoll.started_at >= start,
                SocialMentionPoll.started_at < end,
                SocialMentionPoll.status.in_(("running", "outcome_unknown")),
            )
        )
        or 0
    )
    identity_reads = (
        session.scalar(
            select(func.count())
            .select_from(SocialMentionPoll)
            .where(
                SocialMentionPoll.started_at >= start,
                SocialMentionPoll.started_at < end,
                SocialMentionPoll.identity_read_performed.is_(True),
                SocialMentionPoll.status != "discarded",
            )
        )
        or 0
    )
    return int(completed) + int(reserved) + int(identity_reads)


def _last_poll(session: Session, account_user_id: str) -> SocialMentionPoll | None:
    return session.scalar(
        select(SocialMentionPoll)
        .where(
            SocialMentionPoll.account_user_id == account_user_id,
            SocialMentionPoll.status == "succeeded",
        )
        .order_by(SocialMentionPoll.started_at.desc(), SocialMentionPoll.id.desc())
        .limit(1)
    )


def _latest_poll(session: Session, account_user_id: str) -> SocialMentionPoll | None:
    return session.scalar(
        select(SocialMentionPoll)
        .where(SocialMentionPoll.account_user_id == account_user_id)
        .order_by(SocialMentionPoll.started_at.desc(), SocialMentionPoll.id.desc())
        .limit(1)
    )


def poll_mentions(session: Session, now: datetime, *, force: bool = False) -> MentionPollResult:
    """Read one bounded mention page for the configured account and retain IDs only."""
    result = MentionPollResult()
    if settings_store.get(session, "x_replies_enabled") != "yes":
        return result
    try:
        account_id = _account_id(session)
        token = _token(session)
    except SocialReplyError:
        return result
    interval = settings_store.get_int(session, "x_mentions_poll_minutes") or 5
    daily_cap = settings_store.get_int(session, "x_mentions_daily_read_cap") or 100
    last = _last_poll(session, account_id)
    latest = _latest_poll(session, account_id)
    if not force and latest is not None and latest.started_at > now - timedelta(minutes=interval):
        result.deferred += 1
        return result

    stale = list(
        session.scalars(
            select(SocialMentionPoll)
            .where(
                SocialMentionPoll.status == "running",
                SocialMentionPoll.started_at < now - POLL_TIMEOUT,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for poll in stale:
        poll.status = "outcome_unknown"
        poll.finished_at = now
        poll.last_error = "The request stopped before its billed result count could be confirmed."
    if stale:
        session.commit()
        result.unknown += len(stale)

    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("africasignal.social-mentions.daily")))
    )
    start, end = _lagos_day(now)
    remaining = daily_cap - _read_usage(session, start, end)
    token_fingerprint = _token_fingerprint(token)
    identity_read_needed = last is None or last.token_fingerprint != token_fingerprint
    continuing_page = (
        last is not None
        and last.token_fingerprint == token_fingerprint
        and last.next_page_token is not None
    )
    page_token = last.next_page_token if continuing_page and last is not None else None
    if continuing_page and last is not None:
        since_id = last.since_id
        scan_newest_id = last.newest_id
    elif last is not None and last.next_page_token is not None:
        # A changed token invalidates the opaque page token; restart this scan from its watermark.
        since_id = last.since_id
        scan_newest_id = last.since_id
    else:
        since_id = last.newest_id if last is not None else None
        scan_newest_id = since_id
    identity_reserve = 1 if identity_read_needed else 0
    if remaining < 5 + identity_reserve:
        result.deferred += 1
        session.rollback()
        return result
    reserve = min(POLL_MAX_RESULTS, remaining - identity_reserve, 100)
    if reserve < 5:
        result.deferred += 1
        session.rollback()
        return result
    poll = SocialMentionPoll(
        account_user_id=account_id,
        token_fingerprint=token_fingerprint,
        identity_read_performed=identity_read_needed,
        since_id=since_id,
        newest_id=scan_newest_id,
        page_token=page_token,
        status="running",
        reserved_results=reserve,
        fetched_results=0,
        new_mentions=0,
        started_at=now,
    )
    session.add(poll)
    session.flush()
    poll_id = poll.id
    session.commit()  # reserve the read budget before any billable X API request

    current_account = settings_store.get(session, "x_account_user_id") or ""
    current_token = settings_store.get(session, "x_user_access_token") or ""
    if (
        settings_store.get(session, "x_replies_enabled") != "yes"
        or current_account != account_id
        or not current_token
        or _token_fingerprint(current_token) != token_fingerprint
    ):
        _finish_poll(
            session,
            poll_id,
            "discarded",
            now,
            error="Reply intake or its account token changed before dispatch.",
        )
        result.deferred += 1
        return result

    if identity_read_needed:
        try:
            actual_account_id = authenticated_user_id(current_token)
        except XPostRejected as exc:
            _finish_poll(session, poll_id, "failed", now, error=str(exc))
            result.failed += 1
            return result
        except XPostOutcomeUnknown as exc:
            _finish_poll(session, poll_id, "outcome_unknown", now, error=str(exc))
            result.unknown += 1
            return result
        poll_with_identity = session.get(SocialMentionPoll, poll_id)
        if poll_with_identity is not None:
            poll_with_identity.identity_read_performed = True
        if actual_account_id != account_id:
            _finish_poll(
                session,
                poll_id,
                "failed",
                now,
                error="The X user token does not belong to the configured account ID.",
            )
            result.failed += 1
            return result

    try:
        found = get_mentions(current_token, account_id, reserve, since_id, page_token)
    except XPostRejected as exc:
        if page_token is not None and last is not None:
            last.next_page_token = None
            last.newest_id = last.since_id
        _finish_poll(session, poll_id, "failed", now, error=str(exc))
        result.failed += 1
        return result
    except XPostOutcomeUnknown as exc:
        _finish_poll(session, poll_id, "outcome_unknown", now, error=str(exc))
        result.unknown += 1
        return result
    except Exception as exc:
        log.exception("X mentions poll %s failed without a definitive outcome", poll_id)
        _finish_poll(
            session,
            poll_id,
            "outcome_unknown",
            now,
            error=f"Unexpected {type(exc).__name__}; the full read allowance remains reserved.",
        )
        result.unknown += 1
        return result

    if (
        settings_store.get(session, "x_replies_enabled") != "yes"
        or (settings_store.get(session, "x_account_user_id") or "") != account_id
        or _token_fingerprint(settings_store.get(session, "x_user_access_token") or "")
        != token_fingerprint
    ):
        poll_with_response = session.get(SocialMentionPoll, poll_id)
        if poll_with_response is not None:
            poll_with_response.fetched_results = found.fetched_count
        _finish_poll(
            session,
            poll_id,
            "discarded",
            now,
            error=(
                "Reply intake or its account token changed during the request; "
                "results were discarded."
            ),
        )
        result.deferred += 1
        return result

    new_mentions = 0
    poll_for_response = session.scalar(
        select(SocialMentionPoll).where(SocialMentionPoll.id == poll_id).with_for_update()
    )
    if poll_for_response is None:
        session.rollback()
        result.deferred += 1
        return result
    for mention in found.mentions:
        if mention.author_id == account_id:
            continue  # suppress self-mentions and bot loops
        inserted = session.execute(
            pg_insert(SocialMention)
            .values(
                poll_id=poll_id,
                post_id=mention.post_id,
                conversation_id=mention.conversation_id,
                remote_created_at=mention.created_at,
                first_seen_at=now,
                expires_at=now + MENTION_RETENTION,
                status="new",
            )
            .on_conflict_do_nothing(index_elements=[SocialMention.post_id])
            .returning(SocialMention.id)
        ).scalar_one_or_none()
        if inserted is not None:
            new_mentions += 1
    newest = [item for item in (scan_newest_id, found.newest_id) if item is not None]
    poll_for_response.newest_id = max(newest, key=int) if newest else None
    poll_for_response.next_page_token = found.next_token
    poll_for_response.fetched_results = found.fetched_count
    poll_for_response.new_mentions = new_mentions
    poll_for_response.status = "succeeded"
    poll_for_response.finished_at = now
    poll_for_response.last_error = None
    session.commit()
    result.fetched = found.fetched_count
    result.new_mentions = new_mentions
    return result


def _finish_poll(session: Session, poll_id: int, status: str, now: datetime, *, error: str) -> None:
    row = session.scalar(
        select(SocialMentionPoll).where(SocialMentionPoll.id == poll_id).with_for_update()
    )
    if row is None or row.status != "running":
        session.rollback()
        return
    row.status = status
    row.finished_at = now
    row.last_error = error[:300]
    session.commit()


def _grounded_fact_packet(version: AssessmentVersion, eligibility: dict[str, object]) -> bool:
    dependencies = eligibility.get("dependencies")
    if not isinstance(dependencies, list):
        return False
    allowed_ids = {
        item.get("evidence_document_id")
        for item in dependencies
        if isinstance(item, dict) and isinstance(item.get("evidence_document_id"), int)
    }
    facts = [fact for fact in version.facts if isinstance(fact, dict)]
    return bool(facts) and all(
        isinstance((ids := fact.get("evidence_ids")), list)
        and ids
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item in allowed_ids
            for item in ids
        )
        for fact in facts
    )


def latest_reply_policy(session: Session) -> SocialReplyPolicy | None:
    return session.scalar(
        select(SocialReplyPolicy).order_by(SocialReplyPolicy.revision.desc()).limit(1)
    )


def active_reply_policy(session: Session, account_id: str) -> SocialReplyPolicy | None:
    """Return only an active policy with complete evaluation and both sign-off records."""
    policy = session.scalar(
        select(SocialReplyPolicy).where(SocialReplyPolicy.status == "active").limit(1)
    )
    if (
        policy is None
        or policy.account_user_id != account_id
        or not reply_policy_ops.validate_policy(policy.policy)
        or policy.evaluation_case_set != reply_policy_ops.REQUIRED_EVALUATION_CASE_SET
        or policy.evaluation_case_count is None
        or policy.evaluation_critical_errors is None
        or not (policy.evaluation_evidence_ref or "").strip()
        or not reply_policy_ops.evaluation_is_complete(
            policy.evaluation_case_count,
            policy.evaluation_coverage,
            policy.evaluation_critical_errors,
        )
        or policy.evaluated_by_operator_id is None
        or policy.evaluated_at is None
        or policy.approved_by_operator_id is None
        or policy.approved_at is None
        or not (policy.approval_reason or "").strip()
        or not (policy.staging_evidence_ref or "").strip()
        or policy.activated_by_operator_id is None
        or policy.activated_at is None
        or not (policy.activation_reason or "").strip()
    ):
        return None
    return policy


def _place_country_code(session: Session, place_id: int) -> str | None:
    seen: set[int] = set()
    current_id: int | None = place_id
    for _ in range(10):
        if current_id is None or current_id in seen:
            return None
        seen.add(current_id)
        place = session.get(Place, current_id)
        if place is None:
            return None
        if place.kind == "country":
            return place.code
        current_id = place.parent_id
    return None


def reply_policy_status(session: Session) -> dict[str, object] | None:
    policy = latest_reply_policy(session)
    if policy is None:
        return None
    return {
        "revision": policy.revision,
        "status": policy.status,
        "policy": policy.policy,
        "evaluation_case_set": policy.evaluation_case_set,
        "evaluation_case_count": policy.evaluation_case_count,
        "evaluation_coverage": policy.evaluation_coverage,
        "evaluation_critical_errors": policy.evaluation_critical_errors,
        "evaluation_evidence_ref": policy.evaluation_evidence_ref,
        "approval_reason": policy.approval_reason,
        "staging_evidence_ref": policy.staging_evidence_ref,
        "activation_reason": policy.activation_reason,
        "active": policy.status == "active"
        and active_reply_policy(session, policy.account_user_id or "") is not None,
    }


def create_reply_draft(
    session: Session,
    operator: Operator,
    mention_id: int,
    assessment_version_id: int,
    triage: str,
    *,
    now: datetime,
) -> SocialReply:
    """Draft a deterministic current-fact response after an operator triages the request."""
    if triage not in reply_policy_ops.ALLOWED_CATEGORIES:
        raise SocialReplyError("Only a dated-fact or source request can receive a reply draft.")
    if settings_store.get(session, "x_replies_enabled") != "yes":
        raise SocialReplyError("X mention handling is disabled.")
    if settings_store.get(session, "x_publishing_enabled") != "yes":
        raise SocialReplyError("X publishing is disabled.")
    mention = session.scalar(
        select(SocialMention).where(SocialMention.id == mention_id).with_for_update()
    )
    if (
        mention is None
        or mention.status != "new"
        or mention.expires_at <= now
        or (mention.remote_created_at is not None and mention.remote_created_at > now)
    ):
        raise SocialReplyError("That mention is missing, expired, or already triaged.")
    account_id = _account_id(session)
    poll = session.get(SocialMentionPoll, mention.poll_id)
    if poll is None or poll.account_user_id != account_id:
        raise SocialReplyError("The mention belongs to a different or unverified X account.")
    version = session.scalar(
        select(AssessmentVersion)
        .where(AssessmentVersion.id == assessment_version_id)
        .with_for_update()
    )
    situation = session.get(Situation, version.situation_id) if version else None
    if (
        version is None
        or situation is None
        or version.status != "published"
        or situation.current_version_id != version.id
        or (version.valid_until is not None and version.valid_until <= now)
    ):
        raise SocialReplyError("Choose a current, published assessment.")
    eligibility = publication_eligibility(session, version, channel="reply", now=now)
    if not eligibility.eligible or not _grounded_fact_packet(version, eligibility.snapshot()):
        raise SocialReplyError(
            "The assessment has no current, permissioned fact packet for replies."
        )
    base_url = settings_store.get(session, "public_base_url")
    if not base_url:
        raise SocialReplyError("The public address is not configured.")
    try:
        body = write_post(version, situation, base_url=base_url, channel="x").text.replace(
            "?ref=x", "?ref=x-reply"
        )
    except PostError as exc:
        raise SocialReplyError(str(exc)) from None
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    active_policy = active_reply_policy(session, account_id)
    policy_revision = None
    if active_policy is not None:
        if mention.conversation_id is None:
            raise SocialReplyError(
                "The active reply policy requires a conversation ID to enforce its thread cap."
            )
        session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtext(f"africasignal.x-reply-thread.{mention.conversation_id}")
                )
            )
        )
        start, end = _lagos_day(now)
        session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"africasignal.x-replies.{start}")))
        )
        configured_cap = settings_store.get_int(session, "x_replies_daily_cap")
        daily_cap = min(
            int(active_policy.policy["max_replies_per_lagos_day"]),
            configured_cap if configured_cap is not None else 10,
        )
        daily_usage = _reply_attempt_usage(session, start, end) + _reply_review_reservations(
            session, start, end
        )
        conversation_usage = _conversation_reply_usage(session, mention, exclude_reply_id=None)
        country_code = _place_country_code(session, situation.place_id)
        if (
            not reply_policy_ops.policy_allows(
                active_policy.policy,
                category=triage,
                topic=situation.topic,
                country_code=country_code or "",
                conversation_id_present=True,
                replies_today=daily_usage,
                replies_in_conversation=conversation_usage,
            )
            or daily_usage >= daily_cap
        ):
            raise SocialReplyError("The active X reply policy does not authorize this reply.")
        policy_revision = active_policy.revision
    mention.triage_category = triage
    row = SocialReply(
        mention_id=mention.id,
        assessment_version_id=version.id,
        requested_by_operator_id=operator.id,
        reply_to_post_id=mention.post_id,
        destination_account_id=account_id,
        body=body,
        content_sha256=digest,
        policy_revision=policy_revision,
        eligibility_decision=eligibility.snapshot(),
        status="pending_review",
    )
    session.add(row)
    mention.status = "drafted"
    session.flush()
    audit.record(
        session,
        operator,
        "social_reply.draft_created",
        "social_reply",
        row.id,
        after={
            "mention_id": mention.id,
            "reply_to_post_id": mention.post_id,
            "assessment_version_id": version.id,
            "content_sha256": digest,
            "status": "pending_review",
            "policy_revision": policy_revision,
            "triage_category": triage,
            "publication_authorized": False,
        },
    )
    return row


def triage_mention(
    session: Session,
    operator: Operator,
    mention_id: int,
    status: str,
    category: str | None = None,
) -> SocialMention:
    if status not in {"escalated", "dismissed", "unavailable"}:
        raise SocialReplyError("Choose escalate, dismiss, or mark unavailable.")
    if category is not None and category not in (
        reply_policy_ops.ALLOWED_CATEGORIES | reply_policy_ops.ESCALATION_CATEGORIES
    ):
        raise SocialReplyError("Choose a known request category.")
    mention = session.scalar(
        select(SocialMention).where(SocialMention.id == mention_id).with_for_update()
    )
    if mention is None or mention.status not in {"new", "drafted"}:
        raise SocialReplyError("That mention is already triaged.")
    reply = session.scalar(
        select(SocialReply).where(SocialReply.mention_id == mention.id).with_for_update()
    )
    if reply is not None and reply.status in {"queued", "sending", "sent", "outcome_unknown"}:
        raise SocialReplyError(
            "An approved or attempted reply must be handled from its reply record."
        )
    if reply is not None and reply.status == "pending_review":
        reply.status = "cancelled"
        reply.last_error = f"Mention triaged as {status} before reply approval."
        audit.record(
            session,
            operator,
            "social_reply.cancelled_by_triage",
            "social_reply",
            reply.id,
            before={"status": "pending_review"},
            after={"status": "cancelled", "mention_status": status},
        )
    before = mention.status
    mention.status = status
    mention.triage_category = category or "other"
    audit.record(
        session,
        operator,
        f"social_mention.{status}",
        "social_mention",
        mention.id,
        before={"status": before, "post_id": mention.post_id},
        after={
            "status": status,
            "post_id": mention.post_id,
            "triage_category": mention.triage_category,
        },
    )
    return mention


def _current_reply_inputs(
    session: Session, reply: SocialReply, now: datetime
) -> tuple[SocialMention, AssessmentVersion, Situation, dict[str, object]]:
    mention = session.get(SocialMention, reply.mention_id)
    version = session.get(AssessmentVersion, reply.assessment_version_id)
    situation = session.get(Situation, version.situation_id) if version else None
    eligibility = (
        publication_eligibility(session, version, channel="reply", now=now)
        if version is not None
        else None
    )
    if (
        mention is None
        or mention.status != "drafted"
        or mention.expires_at <= now
        or mention.post_id != reply.reply_to_post_id
        or version is None
        or situation is None
        or version.status != "published"
        or situation.current_version_id != version.id
        or (version.valid_until is not None and version.valid_until <= now)
        or eligibility is None
        or not eligibility.eligible
        or not _grounded_fact_packet(version, eligibility.snapshot())
        or hashlib.sha256(reply.body.encode("utf-8")).hexdigest() != reply.content_sha256
    ):
        raise SocialReplyError("The mention, reply content, or evidence changed; review it again.")
    return mention, version, situation, eligibility.snapshot()


def _reply_attempt_usage(session: Session, start: datetime, end: datetime) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(SocialReply)
            .where(
                SocialReply.attempt_started_at >= start,
                SocialReply.attempt_started_at < end,
                SocialReply.status.in_(
                    ("sending", "sent", "failed", "cancelled", "outcome_unknown")
                ),
            )
        )
        or 0
    )


def _reply_review_reservations(session: Session, start: datetime, end: datetime) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(SocialReply)
            .where(
                SocialReply.status == "queued",
                or_(
                    and_(SocialReply.reviewed_at >= start, SocialReply.reviewed_at < end),
                    and_(SocialReply.authorized_at >= start, SocialReply.authorized_at < end),
                ),
            )
        )
        or 0
    )


def _conversation_reply_usage(
    session: Session, mention: SocialMention, *, exclude_reply_id: int | None
) -> int:
    if mention.conversation_id is None:
        return 0
    query = (
        select(func.count())
        .select_from(SocialReply)
        .join(SocialMention, SocialMention.id == SocialReply.mention_id)
        .where(
            SocialMention.conversation_id == mention.conversation_id,
            SocialReply.status.in_(("queued", "sending", "sent", "outcome_unknown")),
        )
    )
    if exclude_reply_id is not None:
        query = query.where(SocialReply.id != exclude_reply_id)
    return int(session.scalar(query) or 0)


def review_reply(
    session: Session,
    operator: Operator,
    reply_id: int,
    decision: str,
    reason: str,
    *,
    now: datetime,
) -> SocialReply:
    if decision not in {"queue", "reject"}:
        raise SocialReplyError("Choose queue for X or reject.")
    try:
        reason = clean_reason(reason)
    except AssessmentError as exc:
        raise SocialReplyError(str(exc)) from None
    reply = session.scalar(select(SocialReply).where(SocialReply.id == reply_id).with_for_update())
    if reply is None or reply.status != "pending_review":
        raise SocialReplyError("That reply is no longer awaiting review.")
    mention, _version, situation, eligibility = _current_reply_inputs(session, reply, now)
    before = reply.status
    if decision == "reject":
        reply.status = "rejected"
        mention.status = "escalated"
    else:
        if (
            settings_store.get(session, "x_replies_enabled") != "yes"
            or settings_store.get(session, "x_publishing_enabled") != "yes"
            or publication_suspended(session)
        ):
            raise SocialReplyError("X replies are disabled or publication is suspended.")
        if _account_id(session) != reply.destination_account_id:
            raise SocialReplyError("The configured X account changed after this draft was made.")
        active_policy = active_reply_policy(session, reply.destination_account_id)
        if reply.policy_revision is not None and (
            active_policy is None or active_policy.revision != reply.policy_revision
        ):
            raise SocialReplyError(
                "The policy revision attached to this draft is no longer active."
            )
        if active_policy is not None and reply.policy_revision != active_policy.revision:
            raise SocialReplyError(
                "Recreate this draft under the active reply policy before review."
            )
        if mention.conversation_id is not None:
            session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtext(f"africasignal.x-reply-thread.{mention.conversation_id}")
                    )
                )
            )
            existing = session.scalar(
                select(SocialReply.id)
                .join(SocialMention, SocialMention.id == SocialReply.mention_id)
                .where(
                    SocialMention.conversation_id == mention.conversation_id,
                    SocialReply.status.in_(("queued", "sending", "sent", "outcome_unknown")),
                )
                .limit(1)
            )
            if existing is not None:
                raise SocialReplyError("This conversation already has a queued or delivered reply.")
        start, end = _lagos_day(now)
        session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"africasignal.x-replies.{start}")))
        )
        cap = settings_store.get_int(session, "x_replies_daily_cap") or 10
        if active_policy is not None:
            cap = min(cap, int(active_policy.policy["max_replies_per_lagos_day"]))
            if mention.conversation_id is None or not active_policy.policy.get(
                "requires_individual_reply_review"
            ):
                raise SocialReplyError(
                    "The active policy requires a reviewed, conversation-bound reply."
                )
            if not reply_policy_ops.policy_allows(
                active_policy.policy,
                category=mention.triage_category,
                topic=situation.topic,
                country_code=_place_country_code(session, situation.place_id) or "",
                conversation_id_present=True,
                replies_today=(
                    _reply_attempt_usage(session, start, end)
                    + _reply_review_reservations(session, start, end)
                ),
                replies_in_conversation=_conversation_reply_usage(
                    session, mention, exclude_reply_id=reply.id
                ),
            ):
                raise SocialReplyError("The active X reply policy does not authorize this reply.")
        if (
            _reply_attempt_usage(session, start, end)
            + _reply_review_reservations(session, start, end)
            >= cap
        ):
            raise SocialReplyError("The configured daily X reply cap has been reached.")
        reply.status = "queued"
        reply.reviewed_by_operator_id = operator.id
        reply.reviewed_at = now
        reply.authorized_at = now
        reply.eligibility_decision = eligibility
    session.flush()
    audit.record(
        session,
        operator,
        "social_reply.approved_for_send" if decision == "queue" else "social_reply.rejected",
        "social_reply",
        reply.id,
        before={"status": before},
        after={
            "status": reply.status,
            "reason": reason,
            "mention_id": mention.id,
            "reply_to_post_id": reply.reply_to_post_id,
            "destination_account_id": reply.destination_account_id,
            "content_sha256": reply.content_sha256,
            "policy_revision": reply.policy_revision,
            "triage_category": mention.triage_category,
            "eligibility": eligibility,
        },
    )
    return reply


def _finish_reply(
    session: Session,
    reply_id: int,
    status: str,
    now: datetime,
    *,
    error: str | None = None,
    post_id: str | None = None,
) -> None:
    reply = session.scalar(select(SocialReply).where(SocialReply.id == reply_id).with_for_update())
    if reply is None or reply.status != "sending":
        session.rollback()
        return
    reply.status = status
    reply.sent_at = now if status == "sent" else None
    reply.external_post_id = post_id
    reply.last_error = error[:300] if error else None
    mention = session.get(SocialMention, reply.mention_id)
    if mention is not None:
        mention.status = (
            "replied" if status == "sent" else "unavailable" if status == "failed" else "drafted"
        )
    attempt = session.scalar(
        select(DeliveryAttempt)
        .where(
            DeliveryAttempt.social_reply_id == reply.id,
            DeliveryAttempt.attempt_no == 1,
        )
        .with_for_update()
    )
    if attempt is not None:
        attempt.status = {
            "sent": "sent",
            "failed": "rejected",
            "outcome_unknown": "outcome_unknown",
            "cancelled": "cancelled",
        }[status]
        attempt.provider_receipt_id = post_id
        attempt.completed_at = now
        attempt.outcome_detail = error[:300] if error else None
    session.commit()


def dispatch_pending_replies(
    session: Session, now: datetime, limit: int = MAX_DISPATCH_BATCH
) -> ReplyDispatchResult:
    """Send each operator-approved mention response once, with target and evidence rechecks."""
    result = ReplyDispatchResult()
    if (
        settings_store.get(session, "x_replies_enabled") != "yes"
        or settings_store.get(session, "x_publishing_enabled") != "yes"
        or publication_suspended(session)
    ):
        result.held = (
            session.scalar(
                select(func.count()).select_from(SocialReply).where(SocialReply.status == "queued")
            )
            or 0
        )
        return result

    stale = list(
        session.scalars(
            select(SocialReply)
            .where(
                SocialReply.status == "sending",
                SocialReply.attempt_started_at < now - POLL_TIMEOUT,
            )
            .order_by(SocialReply.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for reply in stale:
        reply.status = "outcome_unknown"
        reply.last_error = "X may have accepted this reply; automatic retry is disabled."
        attempt = session.scalar(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.social_reply_id == reply.id)
            .order_by(DeliveryAttempt.attempt_no.desc())
            .limit(1)
            .with_for_update()
        )
        if attempt is not None:
            attempt.status = "outcome_unknown"
            attempt.completed_at = now
            attempt.outcome_detail = reply.last_error
        result.unknown += 1
    if stale:
        session.commit()

    for _ in range(limit):
        queued_reply = session.scalar(
            select(SocialReply)
            .where(SocialReply.status == "queued")
            .order_by(SocialReply.reviewed_at, SocialReply.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if queued_reply is None:
            break
        try:
            mention, version, situation, eligibility = _current_reply_inputs(
                session, queued_reply, now
            )
            account_id = _account_id(session)
            token = _token(session)
        except SocialReplyError as exc:
            queued_reply.status = "stale"
            queued_reply.last_error = str(exc)[:300]
            result.cancelled += 1
            session.commit()
            continue
        if mention.conversation_id is not None:
            session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtext(f"africasignal.x-reply-thread.{mention.conversation_id}")
                    )
                )
            )
        if mention.conversation_id is None and queued_reply.policy_revision is not None:
            queued_reply.status = "cancelled"
            queued_reply.last_error = (
                "The active policy requires a conversation ID for its thread cap."
            )
            result.cancelled += 1
            session.commit()
            continue
        if _conversation_reply_usage(session, mention, exclude_reply_id=queued_reply.id) > 0:
            queued_reply.status = "cancelled"
            queued_reply.last_error = "This conversation already has a queued or delivered reply."
            result.cancelled += 1
            session.commit()
            continue
        policy = None
        if queued_reply.policy_revision is not None:
            policy = active_reply_policy(session, account_id)
            if (
                policy is None
                or policy.revision != queued_reply.policy_revision
                or queued_reply.reviewed_by_operator_id is None
                or queued_reply.authorized_at is None
            ):
                queued_reply.status = "cancelled"
                queued_reply.last_error = "The authorizing X reply policy is no longer active."
                result.cancelled += 1
                session.commit()
                continue
            if not reply_policy_ops.policy_allows(
                policy.policy,
                category=mention.triage_category,
                topic=situation.topic,
                country_code=_place_country_code(session, situation.place_id) or "",
                conversation_id_present=True,
                replies_today=_reply_attempt_usage(session, *_lagos_day(now)),
                replies_in_conversation=_conversation_reply_usage(
                    session, mention, exclude_reply_id=queued_reply.id
                ),
            ):
                queued_reply.status = "cancelled"
                queued_reply.last_error = (
                    "The active X reply policy no longer authorizes this reply."
                )
                result.cancelled += 1
                session.commit()
                continue
        start, end = _lagos_day(now)
        session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"africasignal.x-replies.{start}")))
        )
        cap = settings_store.get_int(session, "x_replies_daily_cap") or 10
        if policy is not None:
            cap = min(cap, int(policy.policy["max_replies_per_lagos_day"]))
        if _reply_attempt_usage(session, start, end) >= cap:
            result.held += 1
            session.rollback()
            break
        if account_id != queued_reply.destination_account_id:
            queued_reply.status = "cancelled"
            queued_reply.last_error = "The configured X account changed after review."
            result.cancelled += 1
            session.commit()
            continue
        queued_reply.status = "sending"
        queued_reply.attempt_started_at = now
        queued_reply.eligibility_decision = eligibility
        attempt = DeliveryAttempt(
            social_reply_id=queued_reply.id,
            attempt_no=1,
            channel="x",
            content_sha256=queued_reply.content_sha256,
            destination_account_id=queued_reply.destination_account_id,
            target_reference=queued_reply.reply_to_post_id,
            assessment_version_ids=[version.id],
            authorization_snapshot={
                "kind": "bounded_reply_policy" if policy is not None else "operator_review",
                "operator_id": queued_reply.reviewed_by_operator_id,
                "triage_operator_id": queued_reply.requested_by_operator_id,
                "reviewed_at": (
                    queued_reply.reviewed_at.isoformat() if queued_reply.reviewed_at else None
                ),
                "policy_revision": queued_reply.policy_revision,
                "authorized_at": (
                    queued_reply.authorized_at.isoformat() if queued_reply.authorized_at else None
                ),
                "triage_category": mention.triage_category,
                "mention_id": mention.id,
                "eligibility": eligibility,
            },
            status="prepared",
            dedupe_key=f"social-reply:{queued_reply.id}:attempt:1",
            attempted_at=now,
        )
        session.add(attempt)
        reply_id = queued_reply.id
        target_id = queued_reply.reply_to_post_id
        body = queued_reply.body
        session.commit()  # no retry fence and authorization receipt are durable before X writes

        try:
            if (
                settings_store.get(session, "x_replies_enabled") != "yes"
                or settings_store.get(session, "x_publishing_enabled") != "yes"
                or publication_suspended(session)
                or (settings_store.get(session, "x_account_user_id") or "") != account_id
                or (settings_store.get(session, "x_user_access_token") or "") != token
            ):
                _finish_reply(
                    session,
                    reply_id,
                    "cancelled",
                    now,
                    error="X reply permission or account credentials changed before dispatch.",
                )
                result.cancelled += 1
                continue
            if queued_reply.policy_revision is not None:
                current_policy = active_reply_policy(session, account_id)
                if (
                    current_policy is None
                    or current_policy.revision != queued_reply.policy_revision
                ):
                    _finish_reply(
                        session,
                        reply_id,
                        "cancelled",
                        now,
                        error="The X reply policy changed before provider dispatch.",
                    )
                    result.cancelled += 1
                    continue
            actual_account_id = authenticated_user_id(token)
            if actual_account_id != account_id:
                _finish_reply(
                    session,
                    reply_id,
                    "failed",
                    now,
                    error="X token does not belong to the reviewed destination account.",
                )
                result.rejected += 1
                continue
            if not post_is_available(token, target_id):
                _finish_reply(
                    session,
                    reply_id,
                    "failed",
                    now,
                    error="The original mention is no longer available.",
                )
                result.rejected += 1
                continue
            session.expire_all()
            current_reply = session.scalar(
                select(SocialReply)
                .where(SocialReply.id == reply_id, SocialReply.status == "sending")
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current_reply is None:
                result.cancelled += 1
                continue
            try:
                _current_mention, _current_version, _current_situation, current_eligibility = (
                    _current_reply_inputs(session, current_reply, now)
                )
            except SocialReplyError as exc:
                _finish_reply(session, reply_id, "cancelled", now, error=str(exc))
                result.cancelled += 1
                continue
            if current_reply.policy_revision is not None:
                current_policy = active_reply_policy(session, account_id)
                if (
                    current_policy is None
                    or current_policy.revision != current_reply.policy_revision
                ):
                    _finish_reply(
                        session,
                        reply_id,
                        "cancelled",
                        now,
                        error="The X reply policy changed before provider dispatch.",
                    )
                    result.cancelled += 1
                    continue
            current_reply.eligibility_decision = current_eligibility
            current_attempt = session.scalar(
                select(DeliveryAttempt)
                .where(
                    DeliveryAttempt.social_reply_id == current_reply.id,
                    DeliveryAttempt.attempt_no == 1,
                )
                .with_for_update()
            )
            if current_attempt is not None:
                current_attempt.authorization_snapshot = {
                    **current_attempt.authorization_snapshot,
                    "eligibility": current_eligibility,
                }
            session.commit()
            receipt = create_reply(token, body, target_id)
        except XPostRejected as exc:
            _finish_reply(session, reply_id, "failed", now, error=str(exc))
            result.rejected += 1
        except XPostOutcomeUnknown as exc:
            _finish_reply(session, reply_id, "outcome_unknown", now, error=str(exc))
            result.unknown += 1
        except Exception as exc:
            log.exception("X social reply %s failed without a definitive outcome", reply_id)
            _finish_reply(
                session,
                reply_id,
                "outcome_unknown",
                now,
                error=f"Unexpected {type(exc).__name__}; automatic retry is disabled.",
            )
            result.unknown += 1
        else:
            _finish_reply(session, reply_id, "sent", now, post_id=receipt.post_id)
            result.sent += 1
    return result


def mentions(session: Session, limit: int = 50) -> list[SocialMention]:
    return list(
        session.scalars(
            select(SocialMention)
            .where(SocialMention.expires_at > datetime.now(UTC))
            .order_by(SocialMention.first_seen_at.desc(), SocialMention.id.desc())
            .limit(limit)
        )
    )


def replies(session: Session, limit: int = 50) -> list[tuple[SocialReply, SocialMention]]:
    rows = session.execute(
        select(SocialReply, SocialMention)
        .join(SocialMention, SocialMention.id == SocialReply.mention_id)
        .order_by(SocialReply.created_at.desc(), SocialReply.id.desc())
        .limit(limit)
    ).all()
    return [(reply, mention) for reply, mention in rows]


def recent_assessments(
    session: Session, now: datetime, limit: int = 50
) -> list[tuple[AssessmentVersion, Situation]]:
    rows = session.execute(
        select(AssessmentVersion, Situation)
        .join(Situation, Situation.id == AssessmentVersion.situation_id)
        .where(
            AssessmentVersion.status == "published",
            Situation.current_version_id == AssessmentVersion.id,
            (AssessmentVersion.valid_until.is_(None) | (AssessmentVersion.valid_until > now)),
        )
        .order_by(AssessmentVersion.published_at.desc(), AssessmentVersion.id.desc())
        .limit(limit)
    ).all()
    return [(version, situation) for version, situation in rows]


def replyable_assessments(
    session: Session, now: datetime, limit: int = 50
) -> list[tuple[AssessmentVersion, Situation]]:
    rows = recent_assessments(session, now, limit)
    versions = [version for version, _situation in rows]
    decisions = publication_eligibility_many(
        session, versions, channel="reply", now=now, require_current=True
    )
    return [
        (version, situation)
        for version, situation in rows
        if (decision := decisions.get(version.id)) is not None
        and decision.eligible
        and _grounded_fact_packet(version, decision.snapshot())
    ]


def expire(session: Session, now: datetime) -> dict[str, int]:
    """Mark expired mentions unavailable and remove unlinked mention IDs after 30 days."""
    expired_rows = list(
        session.scalars(
            select(SocialMention)
            .where(
                SocialMention.expires_at <= now,
                SocialMention.status.in_(("new", "drafted")),
            )
            .with_for_update(skip_locked=True)
        )
    )
    for mention in expired_rows:
        if (
            mention.status == "drafted"
            and session.scalar(
                select(SocialReply.id).where(
                    SocialReply.mention_id == mention.id,
                    SocialReply.status.in_(("queued", "sending", "sent", "outcome_unknown")),
                )
            )
            is not None
        ):
            continue
        mention.status = "unavailable"
    removed_result = session.execute(
        delete(SocialMention).where(
            SocialMention.expires_at <= now - MENTION_RETENTION,
            ~select(SocialReply.id).where(SocialReply.mention_id == SocialMention.id).exists(),
        )
    )
    removed = getattr(removed_result, "rowcount", 0) or 0
    old_polls_result = session.execute(
        delete(SocialMentionPoll).where(
            SocialMentionPoll.created_at < now - timedelta(days=90),
            ~select(SocialMention.id).where(SocialMention.poll_id == SocialMentionPoll.id).exists(),
            SocialMentionPoll.status.in_(("succeeded", "failed", "discarded", "outcome_unknown")),
        )
    )
    old_polls = getattr(old_polls_result, "rowcount", 0) or 0
    return {
        "expired": len(expired_rows),
        "mentions_removed": int(removed),
        "polls_removed": int(old_polls),
    }


def poll_status(session: Session, now: datetime) -> dict[str, object]:
    account_id = settings_store.get(session, "x_account_user_id") or ""
    latest = _last_poll(session, account_id) if account_id else None
    start, end = _lagos_day(now)
    return {
        "enabled": settings_store.get(session, "x_replies_enabled") == "yes",
        "account_configured": account_id.isdigit() and len(account_id) <= 19,
        "token_configured": settings_store.resolve(session, "x_user_access_token").value
        is not None,
        "interval_minutes": settings_store.get_int(session, "x_mentions_poll_minutes") or 5,
        "daily_cap": settings_store.get_int(session, "x_mentions_daily_read_cap") or 100,
        "daily_reserved": _read_usage(session, start, end),
        "last_poll": latest,
    }


def poll_history(session: Session, limit: int = 30) -> list[SocialMentionPoll]:
    return list(
        session.scalars(
            select(SocialMentionPoll)
            .order_by(SocialMentionPoll.started_at.desc(), SocialMentionPoll.id.desc())
            .limit(limit)
        )
    )
