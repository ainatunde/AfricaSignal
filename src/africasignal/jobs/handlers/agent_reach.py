"""Durable, bounded Agent Reach submission, polling and cancellation jobs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from africasignal.agent_reach.client import AgentReachClient, RunnerError, RunnerTaskResponse
from africasignal.config import get_settings
from africasignal.evidence.urls import canonicalise
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext, on_dead, register
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.jobs.queue import ClaimedJob
from africasignal.llm.errors import WorkloadUnavailable
from africasignal.models import AgentReachCandidate, AgentReachTask, WorkloadControl
from africasignal.net.netutil import is_safe_public_url
from africasignal.operations.agent_reach import _lock_runner_configuration, runner_readiness

log = logging.getLogger("africasignal.agent_reach")


def _admit(
    session: Session,
    task_id: int,
    now: datetime,
    expected_status: Literal["queued", "running"],
) -> AgentReachTask | None:
    # Lock order matches workload configuration: control row first, then task row. This makes an
    # off/configuration change linearize against dispatch without creating a row-lock deadlock.
    # Dispatches take an exclusive control-row lock so concurrent workers cannot both
    # observe a free remote slot. Polls use a shared lock and remain concurrent.
    control = session.scalar(
        select(WorkloadControl)
        .where(WorkloadControl.name == "agent_reach")
        .with_for_update(read=expected_status != "queued")
    )
    if control is None:
        raise RuntimeError("Agent Reach workload control is missing")
    task = session.scalar(
        select(AgentReachTask).where(AgentReachTask.id == task_id).with_for_update()
    )
    if task is None or task.status != expected_status:
        return None
    if get_settings().agent_reach_deny:
        task.last_error = "Agent Reach is blocked by the deployment kill switch."
        if expected_status == "queued":
            task.status = "cancelled"
            task.finished_at = now
        else:
            task.status = "cancellation_requested"
            if task.external_task_id:
                queue.enqueue(
                    session,
                    "agent_reach_cancel",
                    {"task_id": task.id},
                    dedupe_key=f"agent-reach:cancel:{task.id}:deployment-deny:{int(now.timestamp())}",
                )
        return None
    if not control.enabled:
        raise WorkloadUnavailable(now + timedelta(hours=1), "Agent Reach is switched off")
    schedule = WorkloadSchedule.model_validate(control.schedule)
    if not schedule.allows(now):
        raise WorkloadUnavailable(schedule.next_open(now), "Agent Reach operating window is closed")
    if task.control_revision != control.revision:
        raise WorkloadUnavailable(now + timedelta(minutes=1), "Agent Reach configuration changed")
    _lock_runner_configuration(session)
    readiness = runner_readiness(session)
    if not readiness.get("connection_verified"):
        raise WorkloadUnavailable(
            now + timedelta(minutes=5),
            "Agent Reach runner configuration needs a fresh health check",
        )
    if not readiness.get("capability_available"):
        raise WorkloadUnavailable(
            now + timedelta(minutes=5),
            "Agent Reach runner has no enabled metadata-search capability",
        )
    if expected_status == "queued":
        active_remote = (
            session.scalar(
                select(func.count())
                .select_from(AgentReachTask)
                .where(
                    AgentReachTask.id != task.id,
                    AgentReachTask.status.in_(
                        ("running", "cancellation_requested", "outcome_unknown")
                    ),
                )
            )
            or 0
        )
        raw_limit = readiness.get("max_concurrency")
        active_limit = (
            raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 0
        )
        if active_limit < 1:
            raise WorkloadUnavailable(
                now + timedelta(minutes=5), "Agent Reach runner has no available task capacity"
            )
        if active_remote >= active_limit:
            raise WorkloadUnavailable(
                now + timedelta(minutes=1), "Agent Reach concurrent task limit is occupied"
            )
    return task


def _enqueue_poll(session: Session, task: AgentReachTask, now: datetime) -> None:
    run_at = now + timedelta(seconds=30)
    slot = int(run_at.timestamp())
    queue.enqueue(
        session,
        "agent_reach_poll",
        {"task_id": task.id},
        dedupe_key=f"agent-reach:poll:{task.id}:{slot}",
        run_at=run_at,
    )


def _finish(
    session: Session,
    task: AgentReachTask,
    response: RunnerTaskResponse,
    now: datetime,
) -> None:
    if response.external_task_id != task.external_task_id:
        raise RunnerError("Agent Reach runner task identity changed")
    if response.task_id != str(task.id):
        raise RunnerError("Agent Reach runner returned a result for another task")
    if response.control_generation != task.control_revision:
        raise RunnerError("Agent Reach runner returned a stale control generation")
    if response.config_revision != task.control_revision:
        raise RunnerError("Agent Reach runner returned a stale configuration revision")

    if response.status in ("queued", "running"):
        task.status = "running"
        _enqueue_poll(session, task, now)
        deadline = (task.started_at or now) + timedelta(minutes=30)
        queue.enqueue(
            session,
            "agent_reach_deadline",
            {"task_id": task.id},
            dedupe_key=f"agent-reach:deadline:{task.id}",
            run_at=deadline,
        )
        return
    if response.status == "cancelled":
        task.status = "cancelled"
        task.last_error = None
        task.finished_at = now
        return
    if response.status == "failed":
        task.status = "failed"
        task.last_error = response.error or "Runner reported that this task failed."
        task.finished_at = now
        return
    if response.status == "expired":
        task.status = "expired"
        task.last_error = "Runner task data expired or its deadline elapsed."
        task.finished_at = now
        return
    if len(response.results) > task.max_results:
        raise RunnerError("Agent Reach runner exceeded the result limit")
    for result in response.results:
        if not is_safe_public_url(result.url):
            raise RunnerError("Agent Reach returned a URL that is not publicly reachable")
        canonical = canonicalise(result.url)
        host = (urlsplit(canonical).hostname or "").lower()
        exists = session.scalar(
            select(AgentReachCandidate.id).where(
                AgentReachCandidate.task_id == task.id,
                AgentReachCandidate.canonical_url == canonical,
            )
        )
        if exists is not None:
            continue
        session.add(
            AgentReachCandidate(
                task_id=task.id,
                url=result.url,
                canonical_url=canonical,
                domain=host,
                title=result.title,
                summary=result.summary,
                publisher=result.publisher,
                platform=result.platform,
                backend=result.backend,
                published_at=result.published_at,
                retrieved_at=result.retrieved_at,
                status="pending",
                retention_until=now + timedelta(days=28),
            )
        )
    task.status = "succeeded"
    task.last_error = None
    task.finished_at = now


@register("agent_reach_search")
def agent_reach_search(ctx: JobContext) -> None:
    session = ctx.session
    now = datetime.now(UTC)
    task = _admit(session, int(ctx.job.payload["task_id"]), now, expected_status="queued")
    if task is None:
        return
    try:
        response = AgentReachClient(session, expected_endpoint=task.runner_endpoint).submit(task)
    except RunnerError as exc:
        if str(exc) == "Agent Reach runner is not configured":
            task.status = "failed"
            task.last_error = str(exc)
            task.finished_at = now
            return
        raise
    task.external_task_id = response.external_task_id
    task.started_at = task.started_at or now
    task.status = "running"
    _finish(session, task, response, now)


@register("agent_reach_poll")
def agent_reach_poll(ctx: JobContext) -> None:
    session = ctx.session
    now = datetime.now(UTC)
    task = _admit(session, int(ctx.job.payload["task_id"]), now, expected_status="running")
    if task is None or not task.external_task_id:
        return

    response = AgentReachClient(session, expected_endpoint=task.runner_endpoint).status(
        task.external_task_id
    )
    _finish(session, task, response, now)


@register("agent_reach_reconcile")
def agent_reach_reconcile(ctx: JobContext) -> None:
    """Look up an uncertain submission before any idempotent retry."""
    session = ctx.session
    now = datetime.now(UTC)
    control = session.scalar(
        select(WorkloadControl).where(WorkloadControl.name == "agent_reach").with_for_update()
    )
    task = session.scalar(
        select(AgentReachTask)
        .where(AgentReachTask.id == int(ctx.job.payload["task_id"]))
        .with_for_update()
    )
    if control is None:
        raise RuntimeError("Agent Reach workload control is missing")
    if task is None or task.status != "outcome_unknown" or task.external_task_id:
        return
    _lock_runner_configuration(session)
    response = AgentReachClient(session, expected_endpoint=task.runner_endpoint).find_by_task_id(
        task.id
    )
    if response is None:
        schedule = WorkloadSchedule.model_validate(control.schedule)
        readiness = runner_readiness(session)
        if get_settings().agent_reach_deny:
            task.last_error = "Runner has no task record; AGENT_REACH_DENY blocks automatic retry."
            return
        if not control.enabled:
            task.last_error = "Runner has no task record; enable Agent Reach before a safe retry."
            return
        if not schedule.allows(now):
            task.last_error = (
                "Runner has no task record; retry requires the configured Agent Reach window."
            )
            return
        if not readiness.get("connection_verified") or not readiness.get("capability_available"):
            task.last_error = (
                "Runner has no task record; refresh the runner health check "
                "and capability before retry."
            )
            return
        active = (
            session.scalar(
                select(func.count())
                .select_from(AgentReachTask)
                .where(
                    AgentReachTask.id != task.id,
                    AgentReachTask.status.in_(
                        ("queued", "running", "cancellation_requested", "outcome_unknown")
                    ),
                )
            )
            or 0
        )
        raw_limit = readiness.get("max_concurrency")
        active_limit = (
            raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 0
        )
        if active_limit < 1 or active >= active_limit:
            task.last_error = (
                "Runner has no task record; safe retry is waiting for available runner capacity."
            )
            return
        task.status = "queued"
        task.control_revision = control.revision
        task.finished_at = None
        task.last_error = "Runner confirmed no prior task; safe idempotent retry queued."
        queue.enqueue(
            session,
            "agent_reach_search",
            {"task_id": task.id},
            dedupe_key=f"agent-reach:submit-recovery:{task.id}:{control.revision}:{int(now.timestamp())}",
        )
        return

    if (
        response.task_id != str(task.id)
        or response.control_generation != task.control_revision
        or response.config_revision != task.control_revision
    ):
        raise RunnerError("Agent Reach runner returned mismatched reconciliation identity")
    task.external_task_id = response.external_task_id
    if task.control_revision != control.revision or get_settings().agent_reach_deny:
        task.last_error = (
            "Deployment kill switch is active; remote results are discarded."
            if get_settings().agent_reach_deny
            else "Remote task belongs to a stale control generation; its leads are discarded."
        )
        task.finished_at = now
        if response.status in ("queued", "running"):
            task.status = "cancellation_requested"
            queue.enqueue(
                session,
                "agent_reach_cancel",
                {"task_id": task.id},
                dedupe_key=f"agent-reach:cancel:{task.id}:stale-generation:{control.revision}",
            )
        else:
            task.status = "cancelled"
        return
    task.started_at = task.started_at or now
    _finish(session, task, response, now)


@register("agent_reach_deadline")
def agent_reach_deadline(ctx: JobContext) -> None:
    session = ctx.session
    task = session.scalar(
        select(AgentReachTask)
        .where(AgentReachTask.id == int(ctx.job.payload["task_id"]))
        .with_for_update()
    )
    if task is None or task.status != "running":
        return
    task.status = "cancellation_requested"
    task.last_error = "Runner task reached its 30-minute deadline."
    queue.enqueue(
        session,
        "agent_reach_cancel",
        {"task_id": task.id},
        dedupe_key=f"agent-reach:cancel:{task.id}:deadline",
    )


@register("agent_reach_expire")
def agent_reach_expire(ctx: JobContext) -> None:
    session = ctx.session
    now = datetime.now(UTC)
    session.execute(delete(AgentReachCandidate).where(AgentReachCandidate.retention_until <= now))
    # Unresolved remote tasks remain visible for accounting and cancellation follow-up.
    session.execute(
        delete(AgentReachTask).where(
            AgentReachTask.retention_until <= now,
            AgentReachTask.status.in_(("queued", "succeeded", "failed", "cancelled", "expired")),
        )
    )


@register("agent_reach_cancel")
def agent_reach_cancel(ctx: JobContext) -> None:
    session = ctx.session
    task = session.scalar(
        select(AgentReachTask)
        .where(AgentReachTask.id == int(ctx.job.payload["task_id"]))
        .with_for_update()
    )
    if task is None or task.status != "cancellation_requested":
        return
    remote_state = "cancelled"
    if task.external_task_id:
        remote_state = AgentReachClient(session, expected_endpoint=task.runner_endpoint).cancel(
            task.external_task_id
        )
        if remote_state == "pending":
            run_at = datetime.now(UTC) + timedelta(seconds=30)
            queue.enqueue(
                session,
                "agent_reach_cancel",
                {"task_id": task.id},
                dedupe_key=f"agent-reach:cancel:{task.id}:confirm:{int(run_at.timestamp())}",
                run_at=run_at,
            )
            return
    task.status = "cancelled"
    if task.external_task_id and remote_state == "completed":
        task.last_error = "Runner completed before cancellation; returned leads were discarded."
    task.finished_at = datetime.now(UTC)


def _mark_unknown(job: ClaimedJob, reason: str) -> None:
    from sqlalchemy.orm import Session

    from africasignal.db import get_engine
    from africasignal.models import AuditLog

    task_id = job.payload.get("task_id")
    if task_id is None:
        return
    with Session(get_engine()) as session:
        task = session.scalar(
            select(AgentReachTask).where(AgentReachTask.id == int(task_id)).with_for_update()
        )
        if task is None or task.status in ("succeeded", "failed", "cancelled", "expired"):
            return
        before = task.status
        task.status = "outcome_unknown"
        task.last_error = reason
        task.finished_at = datetime.now(UTC)
        session.add(
            AuditLog(
                operator_id=None,
                action="agent_reach.outcome_unknown",
                target_kind="agent_reach_task",
                target_id=task.id,
                before={"status": before},
                after={"status": task.status, "reason": reason},
            )
        )
        session.commit()


@on_dead("agent_reach_search")
def search_dead(job: ClaimedJob) -> None:
    _mark_unknown(job, "Submission retries ended; check runner idempotency status before retrying.")


@on_dead("agent_reach_poll")
def poll_dead(job: ClaimedJob) -> None:
    _mark_unknown(job, "Runner status could not be confirmed; remote work may still be running.")


@on_dead("agent_reach_cancel")
def cancel_dead(job: ClaimedJob) -> None:
    _mark_unknown(
        job, "Runner cancellation could not be confirmed; remote work may still be running."
    )


@on_dead("agent_reach_reconcile")
def reconcile_dead(job: ClaimedJob) -> None:
    _mark_unknown(job, "Runner reconciliation could not be confirmed; no new search was submitted.")
