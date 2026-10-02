"""Transaction-local invalidation and deduplicated rebuild admission for commercial context."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, true, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from africasignal.config import get_settings
from africasignal.jobs import queue
from africasignal.models import (
    AssessmentVersion,
    CommercialControl,
    ContentContext,
    Situation,
    WorkloadControl,
)


def _invalidate(
    session: Session, statement: ColumnElement[bool], *, reason: str, now: datetime
) -> int:
    if not reason or len(reason) > 100:
        raise ValueError("commercial context invalidation reason is invalid")
    result = cast(
        CursorResult[Any],
        session.execute(
            update(ContentContext)
            .where(statement, ContentContext.invalidated_at.is_(None))
            .values(
                invalidated_at=now.astimezone(UTC),
                invalidation_reason=reason,
                suitability="invalidated",
            )
        ),
    )
    return int(result.rowcount or 0)


def invalidate_situation_contexts(
    session: Session, situation_id: int, *, reason: str, now: datetime
) -> int:
    versions = select(AssessmentVersion.id).where(AssessmentVersion.situation_id == situation_id)
    return _invalidate(
        session,
        ContentContext.assessment_version_id.in_(versions),
        reason=reason,
        now=now,
    )


def invalidate_source_contexts(
    session: Session, source_id: int, *, reason: str, now: datetime
) -> int:
    return _invalidate(
        session,
        ContentContext.source_refs.contains([source_id]),
        reason=reason,
        now=now,
    )


def invalidate_evidence_contexts(
    session: Session, evidence_id: int, *, reason: str, now: datetime
) -> int:
    return _invalidate(
        session,
        ContentContext.evidence_refs.contains([evidence_id]),
        reason=reason,
        now=now,
    )


def invalidate_all_contexts(session: Session, *, reason: str, now: datetime) -> int:
    return _invalidate(session, true(), reason=reason, now=now)


def _rebuilds_enabled(session: Session) -> bool:
    controls = session.get(CommercialControl, 1)
    workload = session.get(WorkloadControl, "commercial")
    return (
        not get_settings().commercial_deny
        and controls is not None
        and controls.global_enabled
        and workload is not None
        and workload.enabled
    )


def enqueue_context_refresh(
    session: Session, assessment_version_id: int, *, now: datetime
) -> int | None:
    """Queue a current published version only when commercial processing is enabled."""
    if not _rebuilds_enabled(session):
        return None
    version = session.get(AssessmentVersion, assessment_version_id)
    if version is None or version.status != "published":
        return None
    situation = session.get(Situation, version.situation_id)
    if (
        situation is None
        or situation.current_version_id != version.id
        or situation.status != "active"
    ):
        return None
    from africasignal.operations.commercial_context import content_ref_for

    content_ref = content_ref_for(session, version.id)
    if content_ref is None:
        return None
    return queue.enqueue(
        session,
        "commercial_context_rebuild",
        {
            "assessment_version_id": content_ref.assessment_version_id,
            "content_hash": content_ref.content_hash,
        },
        dedupe_key=f"commercial-context-target:{version.id}:{content_ref.content_hash}",
        run_at=now,
    )


def enqueue_context_scan(session: Session, *, now: datetime) -> int | None:
    """Queue one bounded scan for the current hourly slot."""
    if not _rebuilds_enabled(session):
        return None
    slot = int(now.timestamp() // 3600)
    return queue.enqueue(
        session,
        "commercial_context_rebuild",
        {"slot": slot, "after_assessment_version_id": 0},
        dedupe_key=f"commercial-context-scan:{slot}:0",
        run_at=now,
    )
