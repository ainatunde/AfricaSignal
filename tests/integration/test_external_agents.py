from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.external_agents.client import AgentHealth, ExternalAgentClient
from africasignal.jobs.handlers.external_agents import external_agent_expire
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.models import (
    AuditLog,
    ExternalAgentProfile,
    ExternalAgentTask,
    Job,
    Operator,
    WorkloadControl,
)
from africasignal.operations.external_agents import (
    ExternalAgentError,
    ProfileDraft,
    TaskRequest,
    cancel_task,
    configure_profile,
    profiles,
    set_profile_enabled,
    submit_task,
)
from africasignal.operations.external_agents import (
    test_profile as run_profile_health_check,
)
from africasignal.operations.workloads import WorkloadChange
from africasignal.operations.workloads import configure as configure_workload
from africasignal.operators import create_operator


def _operator(session: Session, email: str = "agent-admin@example.org") -> Operator:
    operator, _ = create_operator(session, email, "correct horse battery staple", "admin")
    return operator


def _draft(*, daily_cap: Decimal = Decimal("0.10")) -> ProfileDraft:
    return ProfileDraft.model_validate(
        {
            "slug": "review-agent",
            "display_name": "Review agent",
            "endpoint_url": "https://agent.example",
            "credential": "bearer-test-credential",
            "allowed_purposes": ["research"],
            "allowed_domains": ["example.org"],
            "max_steps": 4,
            "timeout_seconds": 90,
            "max_output_bytes": 4096,
            "max_tasks_per_day": 5,
            "max_concurrency": 4,
            "max_cost_per_task_usd": "0.10",
            "max_spend_per_day_usd": str(daily_cap),
        }
    )


def _healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    def health(_client: ExternalAgentClient) -> AgentHealth:
        return AgentHealth(
            status="ok",
            protocol_version="africasignal.external-agent/1",
            agent_id="review-agent",
            capabilities=["research"],
            enforced_limits=[
                "allowed_domains",
                "idempotency_key",
                "max_cost_per_task_usd",
                "max_output_bytes",
                "max_spend_per_day_usd",
                "max_steps",
                "timeout_seconds",
            ],
            max_concurrency=4,
        )

    monkeypatch.setattr(ExternalAgentClient, "health", health)


def _enable_workload(session: Session, operator: Operator) -> WorkloadControl:
    row = session.scalar(select(WorkloadControl).where(WorkloadControl.name == "external_agents"))
    assert row is not None
    schedule = WorkloadSchedule.model_validate(
        {
            "timezone": "Africa/Lagos",
            "windows": [{"days": list(range(7)), "start": "00:00", "end": "24:00"}],
            "max_concurrency": 4,
            "max_items_per_run": 5,
        }
    )
    return configure_workload(
        session,
        operator,
        "external_agents",
        WorkloadChange(enabled=True, schedule=schedule, expected_revision=row.revision),
    )


def _task(
    session: Session,
    operator: Operator,
    profile: ExternalAgentProfile,
    status: str,
    *,
    external_task_id: str | None = None,
) -> ExternalAgentTask:
    workload = session.scalar(
        select(WorkloadControl).where(WorkloadControl.name == "external_agents")
    )
    assert workload is not None
    now = datetime.now(UTC)
    task = ExternalAgentTask(
        profile_slug=profile.slug,
        requested_by_operator_id=operator.id,
        purpose="research",
        objective="Public power outage trends",
        idempotency_key=f"test-external-agent:{status}:{external_task_id or 'unknown'}",
        profile_revision=profile.revision,
        workload_revision=workload.revision,
        reserved_cost_usd=Decimal("0.10"),
        status=status,
        external_task_id=external_task_id,
        deadline_at=now + timedelta(minutes=2),
        retention_until=now + timedelta(days=30),
    )
    session.add(task)
    session.flush()
    return task


def test_profile_credential_is_encrypted_redacted_and_audited(session: Session) -> None:
    operator = _operator(session)
    profile = configure_profile(session, operator, _draft())

    assert profile.enabled is False
    assert profile.credential_ciphertext != "bearer-test-credential"
    visible = profiles(session)[0]
    assert visible["credential_configured"] is True
    assert "credential" not in visible and "credential_ciphertext" not in visible
    action = session.scalars(
        select(AuditLog).where(AuditLog.action == "external_agent.profile.create")
    ).one()
    assert action.after is not None
    assert "credential" not in action.after
    assert "bearer-test-credential" not in repr(action.after)


def test_admission_uses_schedule_concurrency_and_reserved_spend_without_live_calls(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _operator(session)
    profile = configure_profile(session, operator, _draft())
    _healthy(monkeypatch)
    run_profile_health_check(session, operator, profile.slug)
    profile = set_profile_enabled(
        session, operator, profile.slug, enabled=True, expected_revision=profile.revision
    )
    _enable_workload(session, operator)

    first = submit_task(
        session,
        operator,
        profile.slug,
        TaskRequest(purpose="research", objective="Public power outage trends"),
    )
    assert first.status == "pending" and first.reserved_cost_usd == Decimal("0.10")
    with pytest.raises(ExternalAgentError, match="spend cap"):
        submit_task(
            session,
            operator,
            profile.slug,
            TaskRequest(purpose="research", objective="Public fuel price changes"),
        )

    cancel_task(session, operator, first.id)
    assert first.status == "cancelled"
    second = submit_task(
        session,
        operator,
        profile.slug,
        TaskRequest(purpose="research", objective="Public fuel price changes"),
    )
    assert second.status == "pending"
    with pytest.raises(ExternalAgentError, match="spend cap"):
        submit_task(
            session,
            operator,
            profile.slug,
            TaskRequest(purpose="research", objective="Public food price changes"),
        )
    assert session.scalar(select(Job).where(Job.kind == "external_agent_submit")) is not None


def test_disabling_profile_preserves_cancellation_and_reconciliation_jobs(
    session: Session,
) -> None:
    operator = _operator(session)
    profile = configure_profile(session, operator, _draft())
    profile.enabled = True
    session.flush()
    unknown = _task(session, operator, profile, "outcome_unknown")
    running = _task(session, operator, profile, "running", external_task_id="remote-running")

    disabled = set_profile_enabled(
        session, operator, profile.slug, enabled=False, expected_revision=profile.revision
    )

    assert disabled.enabled is False
    assert unknown.status == "cancellation_requested"
    assert running.status == "cancellation_requested"
    kinds = set(session.scalars(select(Job.kind)).all())
    assert "external_agent_reconcile" in kinds
    assert "external_agent_cancel" in kinds


def test_local_deadline_keeps_remote_task_active_until_cancellation_is_confirmed(
    session: Session,
) -> None:
    operator = _operator(session)
    profile = configure_profile(session, operator, _draft())
    remote = _task(session, operator, profile, "running", external_task_id="remote-overdue")
    remote.deadline_at = datetime.now(UTC) - timedelta(seconds=1)

    external_agent_expire(SimpleNamespace(session=session))

    assert remote.status == "cancellation_requested"
    assert remote.finished_at is None
    with pytest.raises(ExternalAgentError, match="active remote tasks"):
        configure_profile(session, operator, _draft(), expected_revision=profile.revision)
    assert (
        session.scalar(
            select(Job).where(
                Job.kind == "external_agent_cancel",
                Job.payload["task_id"].astext == str(remote.id),
            )
        )
        is not None
    )
