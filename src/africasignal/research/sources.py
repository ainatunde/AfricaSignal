"""Approved adapters and durable request reservations; no search result is trusted evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.models import ResearchAttempt, ResearchQuestion, Source
from africasignal.net.request_scope import RequestScope, ScopeRefused
from africasignal.research.policy import read_policy, readiness
from africasignal.sources.permissions import current_permission


def permitted(session: Session, source: Source, now: datetime, *, reuse: bool = False) -> int:
    policy = read_policy(session)
    permission = current_permission(session, source.id)
    if (
        source.id not in policy.source_ids
        or not source.active
        or permission is None
        or not permission.may_collect
        or permission.approved_at is None
        or (permission.review_due_at is not None and permission.review_due_at <= now)
        or (reuse and not permission.may_republish_numbers)
    ):
        raise ScopeRefused("research_source_permission_unavailable")
    return permission.version


def scope_for(
    factory: sessionmaker[Session], question_id: int, source_id: int, job_id: int
) -> RequestScope:
    with factory() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ScopeRefused("source_missing")
        hosts = frozenset(
            urlsplit(url).hostname
            for url in (source.home_url, source.feed_url)
            if url and urlsplit(url).scheme == "https" and urlsplit(url).hostname
        )
    if not hosts:
        raise ScopeRefused("approved_source_host_missing")

    def reserve() -> None:
        with factory() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('research.request_budget'))")
            )
            question = session.scalar(
                select(ResearchQuestion).where(ResearchQuestion.id == question_id).with_for_update()
            )
            now = datetime.now(UTC)
            policy = read_policy(session, lock=True)
            if (
                question is None
                or question.policy_revision != policy.revision
                or question.state not in {"researching", "applying"}
                or question.deadline_at <= now
                or readiness(session, now)[0] != "ready"
            ):
                raise ScopeRefused("research_authority_changed")
            source = session.get(Source, source_id)
            if source is None:
                raise ScopeRefused("source_missing")
            current_hosts = frozenset(
                urlsplit(url).hostname
                for url in (source.home_url, source.feed_url)
                if url and urlsplit(url).scheme == "https" and urlsplit(url).hostname
            )
            if current_hosts != hosts:
                raise ScopeRefused("research_source_host_changed")
            permission_version = permitted(session, source, now)
            used = (
                session.scalar(
                    select(func.count())
                    .select_from(ResearchAttempt)
                    .where(ResearchAttempt.created_at >= now - timedelta(hours=1))
                )
                or 0
            )
            if used >= policy.max_fetches_per_hour or question.fetch_count >= policy.max_attempts:
                raise ScopeRefused("research_request_budget_exhausted")
            question.fetch_count += 1
            session.add(
                ResearchAttempt(
                    question_id=question_id,
                    source_id=source_id,
                    permission_version=permission_version,
                    job_id=job_id,
                    outcome="reserved",
                    detail={"request_reserved": True},
                )
            )
            session.commit()  # reservations survive a killed worker or failed network request

    return RequestScope(frozenset(str(host) for host in hosts), reserve)
