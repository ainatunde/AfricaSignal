"""Revisioned X watch queries, metered polling, and ID-only discovery leads."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.models import (
    Operator,
    SocialListeningLead,
    SocialListeningPoll,
    SocialListeningQuery,
)
from africasignal.publish.x_search import (
    XSearchOutcomeUnknown,
    XSearchRejected,
    search_recent,
)

log = logging.getLogger("africasignal.social_listening")
LAGOS = ZoneInfo("Africa/Lagos")
QUERY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
POLL_TIMEOUT = timedelta(minutes=10)
MAX_QUERIES_PER_RUN = 5
LEAD_RETENTION = timedelta(days=30)
POLL_RETENTION = timedelta(days=90)
MAX_WATCH_QUERIES = 30


class SocialListeningError(ValueError):
    """A safe refusal shown to an operator."""


class SocialListeningConflict(SocialListeningError):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "This watch query changed after the page loaded. Review it and save again."
        )


class QueryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=64)
    query_text: str = Field(min_length=1, max_length=512)
    max_results: int = Field(default=20, ge=10, le=100)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not QUERY_RE.fullmatch(value):
            raise ValueError("use a lowercase name with words separated by hyphens")
        return value

    @field_validator("query_text")
    @classmethod
    def valid_query(cls, value: str) -> str:
        if CONTROL_RE.search(value):
            raise ValueError("query must be one line without control characters")
        return value


@dataclass
class PollResult:
    searched: int = 0
    new_leads: int = 0
    failed: int = 0
    unknown: int = 0
    discarded: int = 0
    deferred: int = 0


def create_query(
    session: Session, operator: Operator, draft: QueryDraft, now: datetime
) -> SocialListeningQuery:
    session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtext(f"africasignal.social-listening.name.{draft.name}")
            )
        )
    )
    session.execute(
        select(
            func.pg_advisory_xact_lock(func.hashtext("africasignal.social-listening.active-limit"))
        )
    )
    if (
        session.scalar(
            select(func.count())
            .select_from(SocialListeningQuery)
            .where(SocialListeningQuery.enabled.is_(True))
        )
        or 0
    ) >= MAX_WATCH_QUERIES:
        raise SocialListeningError(f"at most {MAX_WATCH_QUERIES} watch queries may be enabled")
    if (
        session.scalar(
            select(SocialListeningQuery.id).where(SocialListeningQuery.name == draft.name)
        )
        is not None
    ):
        raise SocialListeningError("a watch query with that name already exists")
    row = SocialListeningQuery(
        name=draft.name,
        query_text=draft.query_text,
        max_results=draft.max_results,
        enabled=True,
        revision=1,
        created_at=now,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "social_listening.query_created",
        "social_listening_query",
        row.id,
        after={"name": row.name, "query_text": row.query_text, "max_results": row.max_results},
    )
    return row


def update_query(
    session: Session,
    operator: Operator,
    query_id: int,
    expected_revision: int,
    draft: QueryDraft,
    enabled: bool,
) -> SocialListeningQuery:
    row = session.scalar(
        select(SocialListeningQuery).where(SocialListeningQuery.id == query_id).with_for_update()
    )
    if row is None:
        raise SocialListeningError("no such watch query")
    if row.revision != expected_revision:
        raise SocialListeningConflict(expected_revision, row.revision)
    if row.name != draft.name:
        session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtext(f"africasignal.social-listening.name.{draft.name}")
                )
            )
        )
    if enabled and not row.enabled:
        session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtext("africasignal.social-listening.active-limit")
                )
            )
        )
    if (
        enabled
        and not row.enabled
        and (
            session.scalar(
                select(func.count())
                .select_from(SocialListeningQuery)
                .where(SocialListeningQuery.enabled.is_(True))
            )
            or 0
        )
        >= MAX_WATCH_QUERIES
    ):
        raise SocialListeningError(f"at most {MAX_WATCH_QUERIES} watch queries may be enabled")
    if (
        row.name != draft.name
        and session.scalar(
            select(SocialListeningQuery.id).where(
                SocialListeningQuery.name == draft.name, SocialListeningQuery.id != row.id
            )
        )
        is not None
    ):
        raise SocialListeningError("a watch query with that name already exists")
    before = {
        "name": row.name,
        "query_text": row.query_text,
        "max_results": row.max_results,
        "enabled": row.enabled,
    }
    changed_search = row.query_text != draft.query_text or row.max_results != draft.max_results
    row.name = draft.name
    row.query_text = draft.query_text
    row.max_results = draft.max_results
    row.enabled = enabled
    if changed_search:
        row.last_seen_post_id = None
        row.last_polled_at = None
    if before != {
        "name": row.name,
        "query_text": row.query_text,
        "max_results": row.max_results,
        "enabled": row.enabled,
    }:
        row.revision += 1
        audit.record(
            session,
            operator,
            "social_listening.query_updated",
            "social_listening_query",
            row.id,
            before=before,
            after={
                "name": row.name,
                "query_text": row.query_text,
                "max_results": row.max_results,
                "enabled": row.enabled,
                "revision": row.revision,
            },
        )
    return row


def set_lead_status(session: Session, operator: Operator, lead_id: int, status: str) -> None:
    if status not in {"reviewed", "dismissed", "unavailable"}:
        raise SocialListeningError("choose reviewed, dismissed, or unavailable")
    row = session.scalar(
        select(SocialListeningLead).where(SocialListeningLead.id == lead_id).with_for_update()
    )
    if row is None:
        raise SocialListeningError("no such lead")
    before = row.status
    row.status = status
    audit.record(
        session,
        operator,
        f"social_listening.lead_{status}",
        "social_listening_lead",
        row.id,
        before={"status": before, "post_id": row.post_id},
        after={"status": status, "post_id": row.post_id},
    )


def _lagos_day(now: datetime) -> tuple[datetime, datetime]:
    local_day = now.astimezone(LAGOS).date()
    return (
        datetime.combine(local_day, time.min, tzinfo=LAGOS).astimezone(UTC),
        datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=LAGOS).astimezone(UTC),
    )


def _daily_reserved(session: Session, start: datetime, end: datetime) -> int:
    completed_reads = (
        session.scalar(
            select(func.coalesce(func.sum(SocialListeningPoll.fetched_results), 0)).where(
                SocialListeningPoll.started_at >= start,
                SocialListeningPoll.started_at < end,
                SocialListeningPoll.status.in_(("succeeded", "discarded")),
            )
        )
        or 0
    )
    active_or_ambiguous = (
        session.scalar(
            select(func.coalesce(func.sum(SocialListeningPoll.reserved_results), 0)).where(
                SocialListeningPoll.started_at >= start,
                SocialListeningPoll.started_at < end,
                SocialListeningPoll.status.in_(("running", "outcome_unknown")),
            )
        )
        or 0
    )
    return int(completed_reads) + int(active_or_ambiguous)


def poll_queries(
    session: Session, now: datetime, *, force: bool = False, operator_id: int | None = None
) -> PollResult:
    """Poll enabled queries once; retain only IDs and drop results from stale revisions."""
    result = PollResult()
    if settings_store.get(session, "x_listening_enabled") != "yes":
        return result
    bearer = settings_store.get(session, "x_app_bearer_token")
    if not bearer:
        return result
    interval = settings_store.get_int(session, "x_listening_poll_minutes") or 360
    daily_cap = settings_store.get_int(session, "x_listening_daily_read_cap") or 100
    day_start, next_day = _lagos_day(now)

    stale = list(
        session.scalars(
            select(SocialListeningPoll)
            .where(
                SocialListeningPoll.status == "running",
                SocialListeningPoll.started_at < now - POLL_TIMEOUT,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for poll in stale:
        poll.status = "outcome_unknown"
        poll.finished_at = now
        poll.last_error = (
            "Search worker stopped during the API request; the full read allowance "
            "remains reserved."
        )
    if stale:
        session.commit()

    query_ids = list(
        session.scalars(
            select(SocialListeningQuery.id)
            .where(SocialListeningQuery.enabled.is_(True))
            .order_by(
                SocialListeningQuery.last_polled_at.asc().nulls_first(), SocialListeningQuery.id
            )
            .limit(MAX_QUERIES_PER_RUN)
        )
    )
    for query_id in query_ids:
        query = session.scalar(
            select(SocialListeningQuery)
            .where(SocialListeningQuery.id == query_id)
            .with_for_update()
        )
        if query is None or not query.enabled:
            session.rollback()
            continue
        if (
            not force
            and query.last_polled_at is not None
            and query.last_polled_at > now - timedelta(minutes=interval)
        ):
            session.rollback()
            continue
        active = session.scalar(
            select(SocialListeningPoll.id).where(
                SocialListeningPoll.query_id == query.id,
                SocialListeningPoll.status == "running",
            )
        )
        if active is not None:
            result.deferred += 1
            session.rollback()
            continue

        session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext("africasignal.x-listening.daily")))
        )
        used = _daily_reserved(session, day_start, next_day)
        remaining = daily_cap - used
        if remaining < 10:
            result.deferred += 1
            session.rollback()
            continue
        reserve = min(query.max_results, remaining, 100)
        if reserve < query.max_results and reserve < 10:
            result.deferred += 1
            session.rollback()
            continue
        poll = SocialListeningPoll(
            query_id=query.id,
            query_revision=query.revision,
            started_by_operator_id=operator_id,
            status="running",
            reserved_results=reserve,
            fetched_results=0,
            new_leads=0,
            started_at=now,
        )
        session.add(poll)
        query.last_polled_at = now
        session.flush()
        poll_id = poll.id
        revision = query.revision
        query_text = query.query_text
        cursor = query.last_seen_post_id
        session.commit()  # reserve budget and fence concurrent polling before the billable request

        # Re-read authorization and configuration after the durable reservation commit. A stop or
        # token rotation made while this job waited must take effect before the network call.
        current_query = session.scalar(
            select(SocialListeningQuery).where(SocialListeningQuery.id == query_id)
        )
        current_token = settings_store.get(session, "x_app_bearer_token")
        if (
            settings_store.get(session, "x_listening_enabled") != "yes"
            or current_query is None
            or not current_query.enabled
            or current_query.revision != revision
            or not current_token
        ):
            _finish_poll(
                session,
                poll_id,
                "failed",
                now,
                error="Listening was disabled or its query/token changed before dispatch.",
            )
            result.deferred += 1
            continue
        try:
            found = search_recent(current_token, query_text, reserve, cursor)
        except XSearchRejected as exc:
            _finish_poll(session, poll_id, "failed", now, error=str(exc))
            result.failed += 1
            continue
        except XSearchOutcomeUnknown as exc:
            _finish_poll(session, poll_id, "outcome_unknown", now, error=str(exc))
            result.unknown += 1
            continue
        except Exception as exc:
            log.exception("X listening poll %s failed without a definitive outcome", poll_id)
            _finish_poll(
                session,
                poll_id,
                "outcome_unknown",
                now,
                error=f"Unexpected {type(exc).__name__}; the read allowance remains reserved.",
            )
            result.unknown += 1
            continue

        current_query = session.scalar(
            select(SocialListeningQuery)
            .where(SocialListeningQuery.id == query_id)
            .with_for_update()
        )
        current_enabled = settings_store.get(session, "x_listening_enabled") == "yes"
        current_is_usable = (
            current_query is not None
            and current_query.revision == revision
            and current_query.enabled
        )
        new_count = 0
        if current_enabled and current_is_usable and current_query is not None:
            for post_id in found.post_ids:
                inserted = session.execute(
                    pg_insert(SocialListeningLead)
                    .values(
                        query_id=query_id,
                        poll_id=poll_id,
                        post_id=post_id,
                        status="new",
                        first_seen_at=now,
                        expires_at=now + LEAD_RETENTION,
                    )
                    .on_conflict_do_nothing(index_elements=[SocialListeningLead.post_id])
                    .returning(SocialListeningLead.id)
                ).scalar_one_or_none()
                if inserted is not None:
                    new_count += 1
            if found.newest_id is not None:
                current_query.last_seen_post_id = found.newest_id
        poll_row = session.get(SocialListeningPoll, poll_id)
        if poll_row is None:
            session.rollback()
            result.discarded += 1
            continue
        poll_row.fetched_results = found.fetched_count
        poll_row.new_leads = new_count
        poll_row.status = "succeeded" if current_enabled and current_is_usable else "discarded"
        poll_row.finished_at = now
        poll_row.last_error = (
            None
            if poll_row.status == "succeeded"
            else "Query or listening setting changed during the request; results were discarded."
        )
        session.commit()
        result.searched += found.fetched_count
        result.new_leads += new_count
        if poll_row.status == "discarded":
            result.discarded += 1
    return result


def _finish_poll(session: Session, poll_id: int, status: str, now: datetime, *, error: str) -> None:
    row = session.scalar(
        select(SocialListeningPoll).where(SocialListeningPoll.id == poll_id).with_for_update()
    )
    if row is None or row.status != "running":
        session.rollback()
        return
    row.status = status
    row.finished_at = now
    row.last_error = error[:300]
    session.commit()


def expire(session: Session, now: datetime) -> dict[str, int]:
    """Remove expired ID-only leads and old poll ledgers; stale requests remain budget-reserved."""
    removed_leads_result = session.execute(
        delete(SocialListeningLead).where(SocialListeningLead.expires_at <= now)
    )
    removed_leads = getattr(removed_leads_result, "rowcount", 0) or 0
    stale = list(
        session.scalars(
            select(SocialListeningPoll)
            .where(
                SocialListeningPoll.status == "running",
                SocialListeningPoll.started_at < now - POLL_TIMEOUT,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for poll in stale:
        poll.status = "outcome_unknown"
        poll.finished_at = now
        poll.last_error = (
            "Search worker stopped during the API request; the full read allowance "
            "remains reserved."
        )
    removed_polls_result = session.execute(
        delete(SocialListeningPoll).where(
            SocialListeningPoll.created_at < now - POLL_RETENTION,
            SocialListeningPoll.status.in_(("succeeded", "failed", "discarded", "outcome_unknown")),
        )
    )
    removed_polls = getattr(removed_polls_result, "rowcount", 0) or 0
    return {"leads": int(removed_leads), "polls": int(removed_polls), "unknown": len(stale)}


def poll_rows(session: Session, limit: int = 20) -> list[SocialListeningPoll]:
    return list(
        session.scalars(
            select(SocialListeningPoll)
            .order_by(SocialListeningPoll.started_at.desc(), SocialListeningPoll.id.desc())
            .limit(limit)
        )
    )


def query_rows(session: Session) -> list[SocialListeningQuery]:
    return list(
        session.scalars(
            select(SocialListeningQuery).order_by(
                SocialListeningQuery.enabled.desc(), SocialListeningQuery.name
            )
        )
    )


def lead_rows(session: Session, limit: int = 50) -> list[tuple[SocialListeningLead, str]]:
    rows = session.execute(
        select(SocialListeningLead, SocialListeningQuery.name)
        .join(SocialListeningQuery, SocialListeningQuery.id == SocialListeningLead.query_id)
        .where(SocialListeningLead.expires_at > datetime.now(UTC))
        .order_by(SocialListeningLead.first_seen_at.desc(), SocialListeningLead.id.desc())
        .limit(limit)
    ).all()
    return [(row[0], row[1]) for row in rows]


def status(session: Session, now: datetime) -> dict[str, object]:
    start, end = _lagos_day(now)
    used = _daily_reserved(session, start, end)
    enabled = settings_store.get(session, "x_listening_enabled") == "yes"
    interval = settings_store.get_int(session, "x_listening_poll_minutes") or 360
    active_queries = [row for row in query_rows(session) if row.enabled]
    next_poll = None
    if enabled and active_queries:
        next_poll = min(
            (
                now
                if row.last_polled_at is None
                else max(now, row.last_polled_at + timedelta(minutes=interval))
                for row in active_queries
            ),
            default=None,
        )
    return {
        "enabled": enabled,
        "token_configured": settings_store.resolve(session, "x_app_bearer_token").value is not None,
        "interval_minutes": interval,
        "daily_cap": settings_store.get_int(session, "x_listening_daily_read_cap") or 100,
        "daily_reserved": used,
        "next_poll": next_poll,
    }
