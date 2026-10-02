"""Versioned, default-off operator controls for commercial capabilities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.config import get_settings
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.models import CommercialControl, Operator, WorkloadControl
from africasignal.publish.versions import publication_suspended

# The Explore renderer is implemented; legal and deployment gates still control activation.
EXPLORE_SPONSORSHIP_READY = True
CONTEXT_AI_READY = False


class CommercialControlChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_enabled: bool
    explore_sponsorship_enabled: bool
    context_ai_enabled: bool
    expected_revision: int = Field(ge=1)


class RevisionConflict(ValueError):
    """The operator edited a stale control revision."""


def _workload_blockers(session: Session, name: str, now: datetime) -> list[str]:
    row = session.get(WorkloadControl, name)
    if row is None:
        return [f"The {name} workload control is missing; apply migrations."]
    blockers: list[str] = []
    if not row.enabled:
        blockers.append(f"The {name} workload is off.")
    schedule = WorkloadSchedule.model_validate(row.schedule)
    if not schedule.allows(now):
        blockers.append(f"The {name} workload is outside its operating window.")
    return blockers


def current(session: Session, *, at: datetime | None = None) -> dict[str, Any]:
    """Return desired/effective state and safe blockers; never expose secrets or contacts."""
    now = at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("commercial control state requires a timezone-aware instant")
    now = now.astimezone(UTC)
    row = session.get(CommercialControl, 1)
    if row is None:
        raise RuntimeError("commercial controls are missing; apply the current database migration")

    settings = get_settings()
    deployment_denied = settings.commercial_deny
    suspended = publication_suspended(session)
    explore_desired = row.global_enabled and row.explore_sponsorship_enabled
    explore_blockers: list[str] = []
    if not row.global_enabled:
        explore_blockers.append("The global commercial switch is off.")
    if not row.explore_sponsorship_enabled:
        explore_blockers.append("Explore sponsorship is off.")
    if deployment_denied:
        explore_blockers.append("Blocked by the deployment-owned COMMERCIAL_DENY flag.")
    if suspended:
        explore_blockers.append("Publication is suspended; sponsored modules remain hidden.")
    if settings_store.get(session, "legal_review_confirmed") != "yes":
        explore_blockers.append("The current legal pages are not marked reviewed.")
    if settings_store.get(session, "commercial_privacy_review_confirmed") != "yes":
        explore_blockers.append(
            "Commercial sponsorship privacy and measurement review is not confirmed."
        )
    if not EXPLORE_SPONSORSHIP_READY:
        explore_blockers.append("The Explore sponsorship renderer is not implemented yet.")

    context_desired = row.global_enabled and row.context_ai_enabled
    context_blockers: list[str] = []
    if not row.global_enabled:
        context_blockers.append("The global commercial switch is off.")
    if not row.context_ai_enabled:
        context_blockers.append("Commercial AI classification is off.")
    if deployment_denied:
        context_blockers.append("Blocked by the deployment-owned COMMERCIAL_DENY flag.")
    context_blockers.extend(_workload_blockers(session, "commercial", now))
    context_blockers.extend(_workload_blockers(session, "ai", now))
    if not CONTEXT_AI_READY:
        context_blockers.append("The commercial AI classification handler is not implemented yet.")

    return {
        "revision": row.revision,
        "global_enabled": row.global_enabled,
        "explore_sponsorship_enabled": row.explore_sponsorship_enabled,
        "context_ai_enabled": row.context_ai_enabled,
        "deployment_denied": deployment_denied,
        "publication_suspended": suspended,
        "explore_desired": explore_desired,
        "explore_effective": explore_desired and not explore_blockers,
        "explore_blockers": explore_blockers,
        "context_ai_desired": context_desired,
        "context_ai_effective": context_desired and not context_blockers,
        "context_ai_blockers": context_blockers,
    }


def configure(
    session: Session, operator: Operator, change: CommercialControlChange
) -> CommercialControl:
    if operator.disabled_at is not None or operator.role != "admin":
        raise PermissionError("commercial controls require an enabled admin operator")
    row = session.scalar(
        select(CommercialControl).where(CommercialControl.singleton_id == 1).with_for_update()
    )
    if row is None:
        raise RuntimeError("commercial controls are missing; apply the current database migration")
    if row.revision != change.expected_revision:
        raise RevisionConflict("commercial controls changed; reload and try again")
    before = {
        "global_enabled": row.global_enabled,
        "explore_sponsorship_enabled": row.explore_sponsorship_enabled,
        "context_ai_enabled": row.context_ai_enabled,
        "revision": row.revision,
    }
    after = {
        "global_enabled": change.global_enabled,
        "explore_sponsorship_enabled": change.explore_sponsorship_enabled,
        "context_ai_enabled": change.context_ai_enabled,
    }
    if all(before[key] == value for key, value in after.items()):
        return row
    row.global_enabled = change.global_enabled
    row.explore_sponsorship_enabled = change.explore_sponsorship_enabled
    row.context_ai_enabled = change.context_ai_enabled
    row.revision += 1
    row.updated_by_operator_id = operator.id
    row.updated_at = datetime.now(UTC)
    audit.record(
        session,
        operator,
        "commercial_control.configure",
        "commercial_control",
        row.singleton_id,
        before=before,
        after={**after, "revision": row.revision},
    )
    session.flush()
    return row
