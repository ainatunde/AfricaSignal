"""Queue handlers for bounded external-agent submission and remote task lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from africasignal.config import get_settings
from africasignal.external_agents.client import (
    AgentTaskReply,
    AmbiguousExternalAgentOutcome,
    ExternalAgentClient,
    ExternalAgentProtocolError,
)
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext, register
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.llm.errors import WorkloadUnavailable
from africasignal.models import ExternalAgentProfile, ExternalAgentTask, WorkloadControl
from africasignal.operations.external_agents import (
    _check_healthy,
    _decrypt,
)

TERMINAL_REMOTE = {"succeeded", "failed", "cancelled", "expired"}


def _load(
    session: Any, task_id: int
) -> tuple[WorkloadControl, ExternalAgentProfile, ExternalAgentTask] | None:
    workload = session.scalar(
        select(WorkloadControl).where(WorkloadControl.name == "external_agents").with_for_update()
    )
    task = session.scalar(
        select(ExternalAgentTask).where(ExternalAgentTask.id == task_id).with_for_update()
    )
    if workload is None or task is None:
        return None
    profile = session.scalar(
        select(ExternalAgentProfile)
        .where(ExternalAgentProfile.slug == task.profile_slug)
        .with_for_update()
    )
    if profile is None:
        task.status = "outcome_unknown"
        task.last_error = (
            "Configured external-agent profile is missing; remote outcome is unresolved."
        )
        return None
    return workload, profile, task


def _client(profile: ExternalAgentProfile) -> ExternalAgentClient:
    return ExternalAgentClient(profile, _decrypt(profile))


def _schedule_poll(session: Any, task: ExternalAgentTask, now: datetime, seconds: int = 30) -> None:
    run_at = now + timedelta(seconds=seconds)
    slot = int(run_at.timestamp()) // seconds
    queue.enqueue(
        session,
        "external_agent_poll",
        {"task_id": task.id},
        dedupe_key=f"external-agent:poll:{task.id}:{seconds}:{slot}",
        run_at=run_at,
    )


def _schedule_cancel(
    session: Any, task: ExternalAgentTask, now: datetime, seconds: int = 15
) -> None:
    run_at = now + timedelta(seconds=seconds)
    queue.enqueue(
        session,
        "external_agent_cancel",
        {"task_id": task.id},
        dedupe_key=f"external-agent:cancel:{task.id}:retry:{int(run_at.timestamp()) // seconds}",
        run_at=run_at,
    )


def _schedule_reconcile(
    session: Any, task: ExternalAgentTask, now: datetime, seconds: int = 30
) -> None:
    run_at = now + timedelta(seconds=seconds)
    queue.enqueue(
        session,
        "external_agent_reconcile",
        {"task_id": task.id},
        dedupe_key=f"external-agent:reconcile:{task.id}:{int(run_at.timestamp()) // seconds}",
        run_at=run_at,
    )


def _safe_error(value: str | None, default: str) -> str:
    if not value:
        return default
    return "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in value)[:300]


def _apply_reply(
    session: Any,
    profile: ExternalAgentProfile,
    task: ExternalAgentTask,
    reply: AgentTaskReply,
    now: datetime,
) -> None:
    if task.external_task_id and task.external_task_id != reply.external_task_id:
        raise ExternalAgentProtocolError("External agent task identity changed")
    previous_status = task.status
    task.external_task_id = reply.external_task_id
    if reply.usage is not None:
        task.reported_usage = {
            "source": "unverified_remote_report",
            **reply.usage.model_dump(mode="json"),
        }
    if reply.status in ("queued", "running"):
        task.started_at = task.started_at or now
        if task.status not in ("cancellation_requested", "expired"):
            task.status = "running"
            _schedule_poll(session, task, now)
        else:
            task.status = "cancellation_requested"
            _schedule_cancel(session, task, now, 15)
        return
    task.finished_at = now
    if reply.status == "succeeded":
        task.status = "succeeded"
        task.result_text = reply.result_text
        task.last_error = (
            "Remote task completed after cancellation or expiry; result remains untrusted."
            if previous_status in ("cancellation_requested", "expired")
            else None
        )
    elif reply.status == "failed":
        task.status = "failed"
        task.last_error = _safe_error(reply.error, "External agent reported task failure.")
    elif reply.status == "cancelled":
        task.status = "cancelled"
        task.last_error = None
    elif reply.status == "expired":
        task.status = "expired"
        task.last_error = _safe_error(reply.error, "External agent task expired.")


def _dispatch_allowed(
    session: Any,
    workload: WorkloadControl,
    profile: ExternalAgentProfile,
    task: ExternalAgentTask,
    now: datetime,
) -> None:
    if task.profile_revision != profile.revision:
        raise ExternalAgentProtocolError("External-agent profile revision changed before dispatch")
    if task.workload_revision != workload.revision:
        raise ExternalAgentProtocolError("External-agent workload revision changed before dispatch")
    if get_settings().external_agents_deny or not workload.enabled or not profile.enabled:
        raise ExternalAgentProtocolError("External-agent dispatch is disabled")
    schedule = WorkloadSchedule.model_validate(workload.schedule)
    if not schedule.allows(now):
        raise WorkloadUnavailable(
            schedule.next_open(now), "External-agent operating window is closed"
        )
    window_end = schedule.current_window_end(now)
    if window_end is None:
        raise ExternalAgentProtocolError("External-agent operating window is closed")
    task.deadline_at = min(task.deadline_at, window_end)
    if task.deadline_at <= now:
        task.status = "expired"
        task.finished_at = now
        task.last_error = "Task cannot finish before the configured workload window closes."
        raise ExternalAgentProtocolError(task.last_error)
    if not _check_healthy(profile, now):
        raise ExternalAgentProtocolError(
            "External-agent health check is stale or the credential changed"
        )
    if task.purpose not in profile.health_capabilities:
        raise ExternalAgentProtocolError("External-agent purpose is no longer available")
    active = int(
        session.scalar(
            select(func.count())
            .select_from(ExternalAgentTask)
            .where(
                ExternalAgentTask.id != task.id,
                ExternalAgentTask.status.in_(
                    ("admitted", "running", "cancellation_requested", "outcome_unknown")
                ),
            )
        )
        or 0
    )
    if active >= schedule.max_concurrency:
        next_check = now + timedelta(seconds=30)
        raise WorkloadUnavailable(next_check, "External-agent workload is at its active-task limit")
    profile_active = int(
        session.scalar(
            select(func.count())
            .select_from(ExternalAgentTask)
            .where(
                ExternalAgentTask.id != task.id,
                ExternalAgentTask.profile_slug == profile.slug,
                ExternalAgentTask.status.in_(
                    ("admitted", "running", "cancellation_requested", "outcome_unknown")
                ),
            )
        )
        or 0
    )
    if profile_active >= min(profile.max_concurrency, profile.health_max_concurrency or 0):
        raise WorkloadUnavailable(
            now + timedelta(seconds=30), "External-agent profile is at its active-task limit"
        )
    if task.deadline_at <= now:
        task.status = "expired"
        task.finished_at = now
        task.last_error = "Task deadline elapsed before dispatch."
        raise ExternalAgentProtocolError(task.last_error)


@register("external_agent_submit")
def external_agent_submit(ctx: JobContext) -> None:
    session = ctx.session
    loaded = _load(session, int(ctx.job.payload["task_id"]))
    if loaded is None:
        return
    workload, profile, task = loaded
    if task.status not in ("pending", "admitted"):
        return
    now = datetime.now(UTC)
    try:
        _dispatch_allowed(session, workload, profile, task, now)
    except WorkloadUnavailable:
        raise
    except ExternalAgentProtocolError as exc:
        if task.status != "expired":
            task.status = "cancelled" if "disabled" in str(exc).lower() else "failed"
            task.finished_at = now
            task.last_error = str(exc)
        return
    task.status = "admitted"
    try:
        reply = _client(profile).submit(task, profile)
    except AmbiguousExternalAgentOutcome as exc:
        task.status = "outcome_unknown"
        task.last_error = str(exc)
        _schedule_reconcile(session, task, now)
        return
    except ExternalAgentProtocolError as exc:
        task.status = "failed"
        task.finished_at = now
        task.last_error = str(exc)
        return
    _apply_reply(session, profile, task, reply, now)


@register("external_agent_poll")
def external_agent_poll(ctx: JobContext) -> None:
    session = ctx.session
    loaded = _load(session, int(ctx.job.payload["task_id"]))
    if loaded is None:
        return
    _, profile, task = loaded
    if task.status not in ("running", "cancellation_requested", "expired"):
        return
    now = datetime.now(UTC)
    if task.deadline_at <= now and task.status == "running":
        task.status = "cancellation_requested"
        task.last_error = "Remote task exceeded its deadline; cancellation was requested."
        if task.external_task_id:
            _schedule_cancel(session, task, now, 1)
        else:
            _schedule_reconcile(session, task, now, 1)
        return
    if not task.external_task_id:
        _schedule_reconcile(session, task, now)
        return
    try:
        reply = _client(profile).status(task.external_task_id, task, profile)
    except ExternalAgentProtocolError as exc:
        task.last_error = _safe_error(str(exc), "External-agent status check failed.")
        if task.status == "cancellation_requested":
            _schedule_cancel(session, task, now, 60)
        else:
            _schedule_poll(session, task, now, 60)
        return
    _apply_reply(session, profile, task, reply, now)


@register("external_agent_reconcile")
def external_agent_reconcile(ctx: JobContext) -> None:
    session = ctx.session
    loaded = _load(session, int(ctx.job.payload["task_id"]))
    if loaded is None:
        return
    workload, profile, task = loaded
    if task.status not in (
        "outcome_unknown",
        "pending",
        "admitted",
        "cancellation_requested",
        "expired",
    ):
        return
    now = datetime.now(UTC)
    try:
        reply = _client(profile).by_idempotency_key(task, profile)
    except ExternalAgentProtocolError as exc:
        task.status = "outcome_unknown"
        task.last_error = _safe_error(str(exc), "External-agent outcome remains unknown.")
        _schedule_reconcile(session, task, now, 60)
        return
    if reply is not None:
        if (
            get_settings().external_agents_deny
            or not workload.enabled
            or not profile.enabled
            or profile.revision != task.profile_revision
            or (task.deadline_at <= now and reply.status in ("queued", "running"))
        ):
            task.status = "cancellation_requested"
        _apply_reply(session, profile, task, reply, now)
        return
    if task.status == "cancellation_requested":
        task.status = "cancelled"
        task.finished_at = now
        task.last_error = "No remote task exists; cancellation is complete."
        return
    if task.deadline_at <= now:
        task.status = "expired"
        task.finished_at = now
        task.last_error = "No remote task was found before the request deadline elapsed."
        return
    if (
        get_settings().external_agents_deny
        or not workload.enabled
        or not profile.enabled
        or profile.revision != task.profile_revision
    ):
        task.status = "cancelled"
        task.finished_at = now
        task.last_error = "No remote task exists and the profile is now disabled."
        return
    task.status = "pending"
    task.workload_revision = workload.revision
    queue.enqueue(
        session,
        "external_agent_submit",
        {"task_id": task.id},
        dedupe_key=f"external-agent:submit-reconcile:{task.id}:{int(now.timestamp())}",
        run_at=now + timedelta(seconds=5),
    )


@register("external_agent_cancel")
def external_agent_cancel(ctx: JobContext) -> None:
    session = ctx.session
    loaded = _load(session, int(ctx.job.payload["task_id"]))
    if loaded is None:
        return
    _, profile, task = loaded
    if task.status not in ("cancellation_requested", "expired"):
        return
    now = datetime.now(UTC)
    if not task.external_task_id:
        _schedule_reconcile(session, task, now, 1)
        return
    try:
        reply = _client(profile).cancel(task.external_task_id, task, profile)
    except ExternalAgentProtocolError as exc:
        task.last_error = _safe_error(str(exc), "Remote cancellation could not be confirmed.")
        task.status = "cancellation_requested"
        if task.external_task_id:
            _schedule_cancel(session, task, now, 60)
        else:
            _schedule_reconcile(session, task, now, 60)
        return
    if reply is None:
        task.status = "cancelled"
        task.finished_at = now
        return
    _apply_reply(session, profile, task, reply, now)


@register("external_agent_expire")
def external_agent_expire(ctx: JobContext) -> None:
    session = ctx.session
    now = datetime.now(UTC)
    overdue = session.scalars(
        select(ExternalAgentTask)
        .where(
            ExternalAgentTask.status.in_(("running", "cancellation_requested")),
            ExternalAgentTask.deadline_at <= now,
        )
        .with_for_update()
        .order_by(ExternalAgentTask.id)
    ).all()
    for task in overdue:
        if task.status == "running":
            task.status = "cancellation_requested"
            task.last_error = "Remote task exceeded its deadline; cancellation was requested."
        if task.external_task_id:
            _schedule_cancel(session, task, now, 1)
        else:
            _schedule_reconcile(session, task, now, 1)
    session.execute(
        delete(ExternalAgentTask).where(
            ExternalAgentTask.retention_until <= now,
            ExternalAgentTask.status.in_(TERMINAL_REMOTE),
        )
    )
