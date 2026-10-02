"""Task submission, cancellation and human review for Agent Reach discovery."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.config import get_settings
from africasignal.jobs import queue
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.models import (
    AgentReachCandidate,
    AgentReachTask,
    Operator,
    Setting,
    Source,
    WorkloadControl,
)
from africasignal.settings_store import get as get_setting
from africasignal.settings_store import get_int as get_setting_int
from africasignal.sources.permissions import current_permission
from africasignal.sources.rss import same_site

QUERY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _lock_runner_configuration(session: Session) -> None:
    keys = (
        "config.agent_reach_endpoint",
        "config.agent_reach_api_key",
        "config.agent_reach_max_tasks_per_day",
    )
    session.scalars(
        select(Setting)
        .where(Setting.key.in_(keys))
        .order_by(Setting.key)
        .with_for_update(read=True)
    ).all()


def _runner_token_fingerprint(token: str) -> str:
    pepper = get_settings().secret_key.encode("utf-8")
    return hmac.new(pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()


class AgentReachError(ValueError):
    """A safe operator-facing refusal from the Agent Reach workflow."""


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    topic: Literal["energy", "food"]
    query: str = Field(min_length=8, max_length=500)
    max_results: int = Field(default=10, ge=1, le=20)


def runner_readiness(session: Session) -> dict[str, object]:
    deployment_denied = get_settings().agent_reach_deny
    row = session.get(WorkloadControl, "agent_reach")
    endpoint = bool(get_setting(session, "agent_reach_endpoint"))
    token_value = get_setting(session, "agent_reach_api_key")
    token = bool(token_value)
    now = datetime.now(UTC)
    daily_task_limit = get_setting_int(session, "agent_reach_max_tasks_per_day") or 10
    if row is None:
        return {
            "configured": False,
            "connection_verified": False,
            "deployment_denied": deployment_denied,
            "state": "Migration required",
            "reason": "Apply the Agent Reach database migrations.",
            "enabled": False,
            "effective": False,
            "revision": 0,
            "active_tasks": 0,
            "max_concurrency": 0,
            "runner_max_concurrency": 0,
            "capability_available": False,
            "backend": None,
            "recent_tasks_24h": 0,
            "daily_task_limit": daily_task_limit,
            "next_open": now,
        }
    schedule = WorkloadSchedule.model_validate(row.schedule)
    active_window = schedule.allows(now)
    effective = False
    check = session.get(Setting, "agent_reach.runner_health")
    check_data = check.value if check is not None and isinstance(check.value, dict) else {}
    checked_at_raw = check_data.get("checked_at")
    checked_at = None
    try:
        checked_at = (
            datetime.fromisoformat(checked_at_raw) if isinstance(checked_at_raw, str) else None
        )
    except ValueError:
        checked_at = None
    recent_tasks_24h = (
        session.scalar(
            select(func.count())
            .select_from(AgentReachTask)
            .where(AgentReachTask.created_at >= now - timedelta(hours=24))
        )
        or 0
    )
    active_tasks = 0
    if row is not None:
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
    connection_verified = bool(
        endpoint
        and token
        and check_data.get("endpoint") == get_setting(session, "agent_reach_endpoint")
        and checked_at is not None
        and checked_at.tzinfo is not None
        and token_value is not None
        and check_data.get("token_fingerprint") == _runner_token_fingerprint(token_value)
        and timedelta(0) <= now - checked_at <= timedelta(hours=24)
    )
    capabilities = check_data.get("capabilities")
    raw_runner_concurrency = check_data.get("max_concurrency")
    runner_max_concurrency = (
        raw_runner_concurrency
        if isinstance(raw_runner_concurrency, int)
        and not isinstance(raw_runner_concurrency, bool)
        and 1 <= raw_runner_concurrency <= 4
        else 0
    )
    capability_available = bool(
        connection_verified
        and isinstance(capabilities, list)
        and "public_search_metadata" in capabilities
        and check_data.get("backend") == "youtube_data_api_v3"
    )
    effective_limit = (
        min(schedule.max_concurrency, runner_max_concurrency) if capability_available else 0
    )
    daily_capacity_available = recent_tasks_24h < daily_task_limit
    concurrency_available = active_tasks < effective_limit
    effective = bool(
        not deployment_denied
        and row.enabled
        and active_window
        and capability_available
        and daily_capacity_available
        and concurrency_available
    )
    if deployment_denied:
        state, reason = (
            "Blocked by deployment",
            "AGENT_REACH_DENY is active in deployment configuration.",
        )
    elif not endpoint or not token:
        state, reason = "Not configured", "Set the isolated runner URL and token in Settings."
    elif not connection_verified:
        state, reason = "Configured; runner unverified", "Run the authenticated connection check."
    elif not capability_available:
        state, reason = (
            "Capability unavailable",
            "The runner does not currently advertise the fixed metadata-search capability.",
        )
    elif not row.enabled:
        state, reason = "Off", "Enable Agent Reach in AI and automation when ready."
    elif not active_window:
        state, reason = (
            "Waiting for window",
            "The workload is enabled but outside its operating window.",
        )
    elif not daily_capacity_available:
        state, reason = (
            "Daily limit reached",
            (f"The rolling 24-hour task limit is reached ({recent_tasks_24h}/{daily_task_limit})."),
        )
    elif not concurrency_available:
        state, reason = (
            "Concurrency limit reached",
            (f"All configured runner slots are occupied ({active_tasks}/{effective_limit})."),
        )
    else:
        state, reason = (
            "Capability available; acceptance pending",
            (
                "The fixed YouTube metadata search is available. Rights, editorial quality, "
                "privacy and cost acceptance remain pending."
            ),
        )
    return {
        "configured": endpoint and token,
        "deployment_denied": deployment_denied,
        "connection_verified": connection_verified,
        "capability_available": capability_available,
        "backend": check_data.get("backend") if connection_verified else None,
        "runner_max_concurrency": runner_max_concurrency if connection_verified else 0,
        "connection_checked_at": checked_at,
        "state": state,
        "reason": reason,
        "enabled": row.enabled,
        "effective": effective,
        "revision": row.revision,
        "active_tasks": active_tasks,
        "max_concurrency": effective_limit,
        "recent_tasks_24h": recent_tasks_24h,
        "daily_task_limit": daily_task_limit,
        "schedule": schedule,
        "next_open": schedule.next_open(now),
    }


def submit(session: Session, operator: Operator, request: TaskSubmission) -> AgentReachTask:
    row = session.scalar(
        select(WorkloadControl).where(WorkloadControl.name == "agent_reach").with_for_update()
    )
    if row is None:
        raise AgentReachError("Agent Reach controls are unavailable; apply database migrations.")
    if get_settings().agent_reach_deny:
        raise AgentReachError("Agent Reach is blocked by the deployment kill switch.")
    _lock_runner_configuration(session)
    if not row.enabled:
        raise AgentReachError("Agent Reach is switched off.")
    schedule = WorkloadSchedule.model_validate(row.schedule)
    now = datetime.now(UTC)
    if not schedule.allows(now):
        raise AgentReachError(
            "Agent Reach is outside its operating window; next open: "
            f"{schedule.next_open(now).isoformat()}."
        )
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
    daily_limit = get_setting_int(session, "agent_reach_max_tasks_per_day") or 10
    started_24h = (
        session.scalar(
            select(func.count())
            .select_from(AgentReachTask)
            .where(AgentReachTask.created_at >= now - timedelta(hours=24))
        )
        or 0
    )
    if started_24h >= daily_limit:
        raise AgentReachError(
            f"Agent Reach reached its rolling 24-hour task limit ({started_24h}/{daily_limit})."
        )
    readiness = runner_readiness(session)
    if not readiness.get("configured"):
        raise AgentReachError("Configure the isolated runner URL and token in Settings first.")
    if not readiness.get("connection_verified"):
        raise AgentReachError(
            "Run a successful runner connection check in the last 24 hours first."
        )
    if not readiness.get("capability_available"):
        raise AgentReachError("The runner has no enabled metadata-search capability.")
    raw_limit = readiness.get("max_concurrency")
    active_limit = (
        raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 0
    )
    if active_limit < 1:
        raise AgentReachError("The runner reports no available task capacity.")
    if active_tasks >= active_limit:
        raise AgentReachError(
            f"Agent Reach is at its concurrent task limit ({active_tasks}/{active_limit})."
        )
    result_cap = min(20, schedule.max_items_per_run)
    if request.max_results > result_cap:
        raise AgentReachError(f"Maximum results for this run is {result_cap}.")
    if QUERY_CONTROL.search(request.query):
        raise AgentReachError("Search query cannot contain control characters.")
    task = AgentReachTask(
        requested_by_operator_id=operator.id,
        topic=request.topic,
        query=request.query,
        max_results=request.max_results,
        control_revision=row.revision,
        runner_endpoint=str(get_setting(session, "agent_reach_endpoint")).rstrip("/"),
        status="queued",
        retention_until=now + timedelta(days=28),
    )
    session.add(task)
    session.flush()
    queue.enqueue(
        session,
        "agent_reach_search",
        {"task_id": task.id},
        dedupe_key=f"agent-reach:submit:{task.id}",
    )
    audit.record(
        session,
        operator,
        "agent_reach.submit",
        "agent_reach_task",
        task.id,
        after={
            "topic": task.topic,
            "max_results": task.max_results,
            "control_revision": task.control_revision,
        },
    )
    return task


def cancel(session: Session, operator: Operator, task_id: int) -> AgentReachTask:
    task = session.scalar(
        select(AgentReachTask).where(AgentReachTask.id == task_id).with_for_update()
    )
    if task is None:
        raise AgentReachError("No such Agent Reach task.")
    before = task.status
    if task.status == "queued":
        task.status = "cancelled"
        task.finished_at = datetime.now(UTC)
    elif task.status == "running":
        task.status = "cancellation_requested"
        if task.external_task_id:
            queue.enqueue(
                session,
                "agent_reach_cancel",
                {"task_id": task.id},
                dedupe_key=(
                    f"agent-reach:cancel:{task.id}:operator:{int(datetime.now(UTC).timestamp())}"
                ),
            )
    else:
        raise AgentReachError(f"Task is already {task.status}.")
    audit.record(
        session,
        operator,
        "agent_reach.cancel",
        "agent_reach_task",
        task.id,
        before={"status": before},
        after={"status": task.status},
    )
    return task


def recover_unknown(session: Session, operator: Operator, task_id: int) -> AgentReachTask:
    control = session.scalar(
        select(WorkloadControl).where(WorkloadControl.name == "agent_reach").with_for_update()
    )
    if control is None:
        raise AgentReachError("Agent Reach controls are unavailable; apply database migrations.")
    task = session.scalar(
        select(AgentReachTask).where(AgentReachTask.id == task_id).with_for_update()
    )
    if task is None or task.status != "outcome_unknown":
        raise AgentReachError("That task has no unresolved runner outcome.")
    before = task.status
    now = datetime.now(UTC)
    if task.external_task_id:
        task.status = "cancellation_requested"
        queue.enqueue(
            session,
            "agent_reach_cancel",
            {"task_id": task.id},
            dedupe_key=f"agent-reach:cancel:{task.id}:recovery:{int(now.timestamp())}",
        )
        action = "agent_reach.recover_cancel"
    else:
        task.last_error = (
            "Operator requested runner reconciliation. No new search is submitted until "
            "the previous outcome is confirmed."
        )
        queue.enqueue(
            session,
            "agent_reach_reconcile",
            {"task_id": task.id},
            dedupe_key=f"agent-reach:reconcile:{task.id}:{int(now.timestamp())}",
        )
        action = "agent_reach.reconcile"
    audit.record(
        session,
        operator,
        action,
        "agent_reach_task",
        task.id,
        before={"status": before},
        after={"status": task.status, "has_external_task_id": task.external_task_id is not None},
    )
    return task


def review_candidate(
    session: Session,
    operator: Operator,
    candidate_id: int,
    *,
    action: Literal["accept", "reject"],
    source_id: int | None = None,
) -> AgentReachCandidate:
    candidate = session.scalar(
        select(AgentReachCandidate).where(AgentReachCandidate.id == candidate_id).with_for_update()
    )
    if candidate is None or candidate.status != "pending":
        raise AgentReachError("That candidate is unavailable or already reviewed.")
    task = session.get(AgentReachTask, candidate.task_id)
    now = datetime.now(UTC)
    if task is None or now >= task.retention_until:
        raise AgentReachError("That candidate has expired.")
    before = {"status": candidate.status, "source_id": candidate.queued_source_id}
    if action == "reject":
        candidate.status = "rejected"
    elif action == "accept":
        source = session.get(Source, source_id) if source_id is not None else None
        permission = current_permission(session, source.id) if source is not None else None
        if (
            source is None
            or source.adapter != "rss"
            or source.kind != "news_outlet"
            or not source.active
            or not source.home_url
            or permission is None
            or not permission.may_collect
            or (permission.review_due_at is not None and permission.review_due_at <= now)
            or not same_site(candidate.url, source.home_url)
        ):
            raise AgentReachError(
                "Choose an active RSS source with current collection permission on the same site."
            )
        candidate.status = "fetch_queued"
        candidate.queued_source_id = source.id
        payload: dict[str, object] = {
            "source_id": source.id,
            "url": candidate.url,
            "title": candidate.title,
        }
        # Agent-supplied publication times are unverified and are deliberately not passed into
        # evidence capture.
        queue.enqueue(
            session,
            "process_document",
            payload,
            dedupe_key=f"agent-reach-candidate:{candidate.id}:{source.id}",
        )
    else:
        raise AgentReachError("Unknown review action.")
    audit.record(
        session,
        operator,
        f"agent_reach.candidate.{action}",
        "agent_reach_candidate",
        candidate.id,
        before=before,
        after={
            "status": candidate.status,
            "source_id": candidate.queued_source_id,
            "domain": candidate.domain,
        },
    )
    return candidate


def tasks(session: Session, limit: int = 50) -> list[AgentReachTask]:
    return list(
        session.scalars(
            select(AgentReachTask).order_by(AgentReachTask.created_at.desc()).limit(limit)
        ).all()
    )


def candidates(
    session: Session, limit: int = 100
) -> list[tuple[AgentReachCandidate, AgentReachTask]]:
    return list(
        session.execute(
            select(AgentReachCandidate, AgentReachTask)
            .join(AgentReachTask, AgentReachTask.id == AgentReachCandidate.task_id)
            .where(
                AgentReachCandidate.status == "pending",
                AgentReachCandidate.retention_until > datetime.now(UTC),
            )
            .order_by(AgentReachCandidate.created_at.desc())
            .limit(limit)
        )
        .tuples()
        .all()
    )


def eligible_sources(session: Session) -> list[Source]:
    result: list[Source] = []
    rows = session.scalars(
        select(Source)
        .where(
            Source.active.is_(True),
            Source.adapter == "rss",
            Source.kind == "news_outlet",
            Source.home_url.is_not(None),
        )
        .order_by(Source.name)
    ).all()
    now = datetime.now(UTC)
    for source in rows:
        permission = current_permission(session, source.id)
        if (
            permission is not None
            and permission.may_collect
            and (permission.review_due_at is None or permission.review_due_at > now)
        ):
            result.append(source)
    return result


def test_runner(session: Session, operator: Operator) -> dict[str, object]:
    from africasignal.agent_reach.client import AgentReachClient

    _lock_runner_configuration(session)

    endpoint = get_setting(session, "agent_reach_endpoint")
    token = get_setting(session, "agent_reach_api_key")
    if not endpoint or not token:
        raise AgentReachError("Configure the isolated runner URL and token in Settings first.")
    try:
        health = AgentReachClient(session).health()
    except RuntimeError as exc:
        raise AgentReachError(str(exc)) from None
    checked_at = datetime.now(UTC)
    row = session.get(Setting, "agent_reach.runner_health")
    data = {
        "endpoint": endpoint,
        "protocol_version": health.protocol_version,
        "capabilities": health.capabilities,
        "backend": health.backend,
        "max_concurrency": health.max_concurrency,
        "token_fingerprint": _runner_token_fingerprint(token),
        "checked_at": checked_at.isoformat(),
    }
    if row is None:
        session.add(Setting(key="agent_reach.runner_health", value=data))
    else:
        row.value = data
        row.updated_at = checked_at
    audit.record(
        session,
        operator,
        "agent_reach.runner_health_check",
        "agent_reach_runner",
        None,
        after={
            "status": health.status,
            "protocol_version": health.protocol_version,
            "capabilities": health.capabilities,
            "backend": health.backend,
            "max_concurrency": health.max_concurrency,
        },
    )
    return {
        "status": health.status,
        "protocol_version": health.protocol_version,
        "capabilities": health.capabilities,
        "backend": health.backend,
        "max_concurrency": health.max_concurrency,
    }
