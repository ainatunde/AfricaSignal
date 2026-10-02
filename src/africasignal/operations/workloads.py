"""Admin operations for versioned, auditable workload controls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.config import get_settings
from africasignal.jobs import queue
from africasignal.jobs.policy import WORKLOAD_KINDS, WorkloadSchedule
from africasignal.models import AgentReachTask, Job, Operator, WorkloadControl

WorkloadName = Literal["ai", "agent_reach", "processing"]


class WorkloadChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    schedule: WorkloadSchedule
    expected_revision: int


class RevisionConflict(ValueError):
    pass


def current(session: Session) -> list[dict[str, object]]:
    rows = session.scalars(select(WorkloadControl).order_by(WorkloadControl.name)).all()
    result: list[dict[str, object]] = []
    now = datetime.now(UTC)
    for row in rows:
        schedule = WorkloadSchedule.model_validate(row.schedule)
        kinds = WORKLOAD_KINDS.get(row.name, ())
        queued = (
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status == "queued", Job.kind.in_(kinds))
            )
            or 0
        )
        running = (
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status == "running", Job.kind.in_(kinds))
            )
            or 0
        )
        active_tasks = None
        if row.name == "agent_reach":
            active_tasks = (
                session.scalar(
                    select(func.count())
                    .select_from(AgentReachTask)
                    .where(
                        AgentReachTask.status.in_(
                            ("queued", "running", "cancellation_requested", "outcome_unknown")
                        )
                    )
                )
                or 0
            )
        active_now = schedule.allows(now)
        deployment_denied = row.name == "agent_reach" and get_settings().agent_reach_deny
        result.append(
            {
                "name": row.name,
                "enabled": row.enabled,
                "schedule": schedule,
                "revision": row.revision,
                "updated_at": row.updated_at,
                "active_now": active_now,
                "next_open": schedule.next_open(now),
                "effective": row.enabled and active_now and not deployment_denied,
                "deployment_denied": deployment_denied,
                "queued": queued,
                "running": running,
                "active_tasks": active_tasks,
            }
        )
    return result


def configure(
    session: Session,
    operator: Operator,
    name: WorkloadName,
    change: WorkloadChange,
) -> WorkloadControl:
    row = session.scalar(
        select(WorkloadControl).where(WorkloadControl.name == name).with_for_update()
    )
    if row is None:
        raise ValueError("workload control is missing; apply database migrations")
    if row.revision != change.expected_revision:
        raise RevisionConflict("settings changed since this page loaded; reload and try again")
    if name == "agent_reach" and change.schedule.max_items_per_run > 20:
        raise ValueError("Agent Reach results per task cannot exceed 20")
    if name == "agent_reach" and change.schedule.max_concurrency > 4:
        raise ValueError("Agent Reach runner concurrency cannot exceed 4")
    before = {
        "enabled": row.enabled,
        "schedule": row.schedule,
        "revision": row.revision,
    }
    new_schedule = change.schedule.model_dump(mode="json")
    if row.enabled == change.enabled and row.schedule == new_schedule:
        return row
    row.enabled = change.enabled
    row.schedule = new_schedule
    row.revision += 1
    row.updated_at = datetime.now(UTC)
    if name == "agent_reach":
        tasks = session.scalars(
            select(AgentReachTask)
            .where(AgentReachTask.status.in_(("queued", "running")))
            .with_for_update()
        ).all()
        for task in tasks:
            if task.status == "queued" and change.enabled:
                task.control_revision = row.revision
            elif task.status == "running":
                task.status = "cancellation_requested"
                if task.external_task_id:
                    queue.enqueue(
                        session,
                        "agent_reach_cancel",
                        {"task_id": task.id},
                        dedupe_key=f"agent-reach:cancel:{task.id}:{row.revision}",
                    )
    audit.record(
        session,
        operator,
        "workload.configure",
        f"workload:{name}",
        None,
        before=before,
        after={"enabled": row.enabled, "schedule": row.schedule, "revision": row.revision},
    )
    session.flush()
    return row
