"""Configuration, admission, and supervision for compatible external-agent services."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.config import get_settings
from africasignal.external_agents.client import (
    REQUIRED_ENFORCED_LIMITS,
    AgentHealth,
    ExternalAgentClient,
    ExternalAgentProtocolError,
)
from africasignal.jobs import queue
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.models import (
    ExternalAgentProfile,
    ExternalAgentTask,
    Operator,
    WorkloadControl,
)
from africasignal.operators import derive_key

ACTIVE_TASK_STATUSES = (
    "pending",
    "admitted",
    "running",
    "cancellation_requested",
    "outcome_unknown",
)
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
URL_RE = re.compile(r"(?i)\bhttps?://\S+")


class ExternalAgentError(ValueError):
    """A safe error that can be shown to the administrator."""


class ProfileDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    display_name: str = Field(min_length=1, max_length=100)
    endpoint_url: str = Field(min_length=12, max_length=500)
    credential: str | None = Field(default=None, min_length=8, max_length=500)
    allowed_purposes: list[str] = Field(min_length=1, max_length=3)
    allowed_domains: list[str] = Field(min_length=1, max_length=20)
    max_steps: int = Field(default=4, ge=1, le=10)
    timeout_seconds: int = Field(default=120, ge=1, le=300)
    max_output_bytes: int = Field(default=16000, ge=1024, le=65536)
    max_tasks_per_day: int = Field(default=5, ge=1, le=100)
    max_concurrency: int = Field(default=1, ge=1, le=4)
    max_cost_per_task_usd: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("100"))
    max_spend_per_day_usd: Decimal = Field(default=Decimal("0.50"), gt=0, le=Decimal("1000"))

    @field_validator("endpoint_url")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
            or len(value) > 500
        ):
            raise ValueError(
                "endpoint must be an HTTPS origin without credentials, path, query or fragment"
            )
        hostname = parts.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError("endpoint must use a public DNS name")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("use a public DNS name, not an IP address")
        return f"https://{hostname}" + (f":{parts.port}" if parts.port else "")

    @field_validator("credential")
    @classmethod
    def valid_credential(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(ch.isspace() for ch in value)):
            raise ValueError("credential must be non-empty and contain no spaces")
        return value

    @field_validator("allowed_purposes")
    @classmethod
    def validate_purposes(cls, value: list[str]) -> list[str]:
        allowed = {"research", "summarize", "classify"}
        if len(set(value)) != len(value) or not set(value).issubset(allowed):
            raise ValueError(
                "purposes must be unique and selected from research, summarize, classify"
            )
        return value

    @field_validator("allowed_domains")
    @classmethod
    def valid_domains(cls, value: list[str]) -> list[str]:
        domains: list[str] = []
        for raw in value:
            domain = raw.strip().rstrip(".").lower()
            try:
                domain = domain.encode("idna").decode("ascii")
            except UnicodeError:
                raise ValueError("allowed domains must be valid public domain names") from None
            if not DOMAIN_RE.fullmatch(domain) or domain.endswith(
                (".localhost", ".local", ".internal")
            ):
                raise ValueError("allowed domains must be exact public DNS names without wildcards")
            try:
                ipaddress.ip_address(domain)
            except ValueError:
                pass
            else:
                raise ValueError("allowed domains must be DNS names, not IP addresses")
            domains.append(domain)
        if len(set(domains)) != len(domains):
            raise ValueError("allowed domains must not be repeated")
        return domains

    @model_validator(mode="after")
    def valid_budget(self) -> ProfileDraft:
        if self.max_spend_per_day_usd < self.max_cost_per_task_usd:
            raise ValueError("daily spend cap must be at least the per-task cap")
        return self


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=100)
    endpoint_url: str = Field(min_length=12, max_length=500)
    credential: str | None = Field(default=None, min_length=8, max_length=500)
    allowed_purposes: list[str] = Field(min_length=1, max_length=3)
    allowed_domains: list[str] = Field(min_length=1, max_length=20)
    max_steps: int = Field(ge=1, le=10)
    timeout_seconds: int = Field(ge=1, le=300)
    max_output_bytes: int = Field(ge=1024, le=65536)
    max_tasks_per_day: int = Field(ge=1, le=100)
    max_concurrency: int = Field(ge=1, le=4)
    max_cost_per_task_usd: Decimal = Field(gt=0, le=Decimal("100"))
    max_spend_per_day_usd: Decimal = Field(gt=0, le=Decimal("1000"))
    expected_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_profile(self) -> ProfileUpdate:
        ProfileDraft.model_validate(
            {
                "slug": "profile-check",
                "display_name": self.display_name,
                "endpoint_url": self.endpoint_url,
                "credential": self.credential,
                "allowed_purposes": self.allowed_purposes,
                "allowed_domains": self.allowed_domains,
                "max_steps": self.max_steps,
                "timeout_seconds": self.timeout_seconds,
                "max_output_bytes": self.max_output_bytes,
                "max_tasks_per_day": self.max_tasks_per_day,
                "max_concurrency": self.max_concurrency,
                "max_cost_per_task_usd": self.max_cost_per_task_usd,
                "max_spend_per_day_usd": self.max_spend_per_day_usd,
            }
        )
        return self


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    purpose: str
    objective: str = Field(min_length=8, max_length=2000)

    @field_validator("purpose")
    @classmethod
    def valid_purpose(cls, value: str) -> str:
        if value not in {"research", "summarize", "classify"}:
            raise ValueError("purpose must be research, summarize or classify")
        return value

    @field_validator("objective")
    @classmethod
    def public_query_only(cls, value: str) -> str:
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise ValueError("task objective contains unsupported control characters")
        if URL_RE.search(value) or EMAIL_RE.search(value):
            raise ValueError(
                "submit a public research objective, not URLs or personal contact details"
            )
        return value.strip()


class RevisionConflict(ValueError):
    pass


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_key("external-agent-profile-credential")))


def _encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def _decrypt(profile: ExternalAgentProfile) -> str:
    try:
        return _fernet().decrypt(profile.credential_ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise ExternalAgentError(
            "External-agent credential cannot be decrypted; rotate it."
        ) from None


def _token_fingerprint(token: str) -> str:
    return hmac.new(
        derive_key("external-agent-token-fingerprint"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _active_count(session: Session, slug: str | None = None) -> int:
    query = (
        select(func.count())
        .select_from(ExternalAgentTask)
        .where(ExternalAgentTask.status.in_(ACTIVE_TASK_STATUSES))
    )
    if slug is not None:
        query = query.where(ExternalAgentTask.profile_slug == slug)
    return int(session.scalar(query) or 0)


def _check_healthy(profile: ExternalAgentProfile, now: datetime) -> bool:
    if (
        profile.health_checked_at is None
        or profile.health_checked_at.tzinfo is None
        or not (timedelta(0) <= now - profile.health_checked_at <= timedelta(hours=24))
    ):
        return False
    try:
        token = _decrypt(profile)
    except ExternalAgentError:
        return False
    return bool(
        hmac.compare_digest(profile.health_token_fingerprint or "", _token_fingerprint(token))
        and REQUIRED_ENFORCED_LIMITS.issubset(set(profile.health_enforced_limits or []))
        and set(profile.health_capabilities or []).issubset(set(profile.allowed_purposes))
    )


def _profile_state(profile: ExternalAgentProfile, now: datetime) -> str:
    if get_settings().external_agents_deny:
        return "Blocked by deployment"
    if not profile.enabled:
        return "Off"
    if not _check_healthy(profile, now):
        return "Health check required"
    return "Configured; supervised pilot only"


def profiles(session: Session) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    rows = session.scalars(
        select(ExternalAgentProfile).order_by(ExternalAgentProfile.created_at.desc())
    ).all()
    result = []
    for profile in rows:
        recent = now - timedelta(hours=24)
        recent_tasks = int(
            session.scalar(
                select(func.count())
                .select_from(ExternalAgentTask)
                .where(
                    ExternalAgentTask.profile_slug == profile.slug,
                    ExternalAgentTask.created_at >= recent,
                )
            )
            or 0
        )
        result.append(
            {
                "slug": profile.slug,
                "display_name": profile.display_name,
                "endpoint_url": profile.endpoint_url,
                "credential_configured": bool(profile.credential_ciphertext),
                "allowed_purposes": profile.allowed_purposes,
                "allowed_domains": profile.allowed_domains,
                "max_steps": profile.max_steps,
                "timeout_seconds": profile.timeout_seconds,
                "max_output_bytes": profile.max_output_bytes,
                "max_tasks_per_day": profile.max_tasks_per_day,
                "max_concurrency": profile.max_concurrency,
                "max_cost_per_task_usd": str(profile.max_cost_per_task_usd),
                "max_spend_per_day_usd": str(profile.max_spend_per_day_usd),
                "enabled": profile.enabled,
                "revision": profile.revision,
                "health_checked_at": profile.health_checked_at.isoformat()
                if profile.health_checked_at
                else None,
                "health_current": _check_healthy(profile, now),
                "health_capabilities": profile.health_capabilities or [],
                "health_enforced_limits": profile.health_enforced_limits or [],
                "health_max_concurrency": profile.health_max_concurrency,
                "active_tasks": _active_count(session, profile.slug),
                "recent_tasks_24h": recent_tasks,
                "state": _profile_state(profile, now),
            }
        )
    return result


def _get_profile(session: Session, slug: str, *, lock: bool = False) -> ExternalAgentProfile:
    query = select(ExternalAgentProfile).where(ExternalAgentProfile.slug == slug)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None:
        raise ExternalAgentError("No such external-agent profile.")
    return row


def _lock_workload(session: Session) -> WorkloadControl:
    row = session.scalar(
        select(WorkloadControl).where(WorkloadControl.name == "external_agents").with_for_update()
    )
    if row is None:
        raise ExternalAgentError("External-agent controls are unavailable; apply migrations.")
    return row


def configure_profile(
    session: Session,
    operator: Operator,
    draft: ProfileDraft,
    *,
    expected_revision: int | None = None,
) -> ExternalAgentProfile:
    _lock_workload(session)
    profile = session.scalar(
        select(ExternalAgentProfile)
        .where(ExternalAgentProfile.slug == draft.slug)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if profile is None:
        if expected_revision is not None:
            raise ExternalAgentError("No such external-agent profile.")
        if draft.credential is None:
            raise ExternalAgentError("A credential is required for a new profile.")
        profile = ExternalAgentProfile(
            slug=draft.slug,
            display_name=draft.display_name,
            endpoint_url=draft.endpoint_url,
            credential_ciphertext=_encrypt(draft.credential),
            allowed_purposes=list(draft.allowed_purposes),
            allowed_domains=list(draft.allowed_domains),
            max_steps=draft.max_steps,
            timeout_seconds=draft.timeout_seconds,
            max_output_bytes=draft.max_output_bytes,
            max_tasks_per_day=draft.max_tasks_per_day,
            max_concurrency=draft.max_concurrency,
            max_cost_per_task_usd=draft.max_cost_per_task_usd,
            max_spend_per_day_usd=draft.max_spend_per_day_usd,
            enabled=False,
            revision=1,
        )
        session.add(profile)
        session.flush()
        audit.record(
            session,
            operator,
            "external_agent.profile.create",
            "external_agent_profile",
            None,
            after={
                "slug": profile.slug,
                "enabled": False,
                "revision": profile.revision,
                "purposes": profile.allowed_purposes,
                "domains": profile.allowed_domains,
                "credential_configured": True,
            },
        )
        return profile

    if expected_revision is None or profile.revision != expected_revision:
        raise RevisionConflict("profile changed since this page loaded; reload and try again")
    if profile.enabled:
        raise ExternalAgentError("Disable the profile before changing its endpoint, key or limits.")
    if _active_count(session, profile.slug):
        raise ExternalAgentError(
            "Resolve or cancel active remote tasks before editing this profile."
        )
    before = {
        "endpoint_url": profile.endpoint_url,
        "purposes": profile.allowed_purposes,
        "domains": profile.allowed_domains,
        "max_steps": profile.max_steps,
        "timeout_seconds": profile.timeout_seconds,
        "max_output_bytes": profile.max_output_bytes,
        "max_tasks_per_day": profile.max_tasks_per_day,
        "max_concurrency": profile.max_concurrency,
        "max_cost_per_task_usd": str(profile.max_cost_per_task_usd),
        "max_spend_per_day_usd": str(profile.max_spend_per_day_usd),
        "credential_configured": bool(profile.credential_ciphertext),
        "revision": profile.revision,
    }
    profile.display_name = draft.display_name
    profile.endpoint_url = draft.endpoint_url
    if draft.credential is not None:
        profile.credential_ciphertext = _encrypt(draft.credential)
    profile.allowed_purposes = list(draft.allowed_purposes)
    profile.allowed_domains = list(draft.allowed_domains)
    profile.max_steps = draft.max_steps
    profile.timeout_seconds = draft.timeout_seconds
    profile.max_output_bytes = draft.max_output_bytes
    profile.max_tasks_per_day = draft.max_tasks_per_day
    profile.max_concurrency = draft.max_concurrency
    profile.max_cost_per_task_usd = draft.max_cost_per_task_usd
    profile.max_spend_per_day_usd = draft.max_spend_per_day_usd
    profile.revision += 1
    profile.updated_at = now
    profile.health_checked_at = None
    profile.health_token_fingerprint = None
    profile.health_capabilities = []
    profile.health_enforced_limits = []
    profile.health_max_concurrency = None
    audit.record(
        session,
        operator,
        "external_agent.profile.update",
        "external_agent_profile",
        None,
        before={"slug": profile.slug, **before},
        after={
            "slug": profile.slug,
            "endpoint_url": profile.endpoint_url,
            "purposes": profile.allowed_purposes,
            "domains": profile.allowed_domains,
            "revision": profile.revision,
            "credential_rotated": draft.credential is not None,
        },
    )
    session.flush()
    return profile


def set_profile_enabled(
    session: Session,
    operator: Operator,
    slug: str,
    *,
    enabled: bool,
    expected_revision: int,
) -> ExternalAgentProfile:
    _lock_workload(session)
    profile = _get_profile(session, slug, lock=True)
    if profile.revision != expected_revision:
        raise RevisionConflict("profile changed since this page loaded; reload and try again")
    if profile.enabled == enabled:
        return profile
    now = datetime.now(UTC)
    if enabled and not _check_healthy(profile, now):
        raise ExternalAgentError(
            "Run a successful, current health check before enabling this profile."
        )
    before = {"enabled": profile.enabled, "revision": profile.revision}
    profile.enabled = enabled
    profile.revision += 1
    profile.updated_at = now
    if not enabled:
        tasks = session.scalars(
            select(ExternalAgentTask)
            .where(
                ExternalAgentTask.profile_slug == slug,
                ExternalAgentTask.status.in_(ACTIVE_TASK_STATUSES),
            )
            .with_for_update()
            .order_by(ExternalAgentTask.id)
        ).all()
        for task in tasks:
            if task.status in ("pending", "admitted"):
                task.status = "cancelled"
                task.finished_at = now
                task.last_error = "External-agent profile disabled before dispatch."
            elif task.status in ("running", "outcome_unknown"):
                task.status = "cancellation_requested"
                kind = (
                    "external_agent_cancel" if task.external_task_id else "external_agent_reconcile"
                )
                queue.enqueue(
                    session,
                    kind,
                    {"task_id": task.id},
                    dedupe_key=f"external-agent:{kind}:{task.id}:profile:{profile.revision}",
                )
    audit.record(
        session,
        operator,
        "external_agent.profile.enable" if enabled else "external_agent.profile.disable",
        "external_agent_profile",
        None,
        before={"slug": slug, **before},
        after={"slug": slug, "enabled": profile.enabled, "revision": profile.revision},
    )
    session.flush()
    return profile


def test_profile(
    session: Session, operator: Operator, slug: str
) -> tuple[ExternalAgentProfile, AgentHealth]:
    _lock_workload(session)
    profile = _get_profile(session, slug, lock=True)
    token = _decrypt(profile)
    try:
        health = ExternalAgentClient(profile, token).health()
    except ExternalAgentProtocolError as exc:
        audit.record(
            session,
            operator,
            "external_agent.profile.health_failed",
            "external_agent_profile",
            None,
            after={"slug": slug, "error": str(exc), "revision": profile.revision},
        )
        raise ExternalAgentError(str(exc)) from None
    profile.health_checked_at = datetime.now(UTC)
    profile.health_token_fingerprint = _token_fingerprint(token)
    profile.health_capabilities = list(health.capabilities)
    profile.health_enforced_limits = list(health.enforced_limits)
    profile.health_max_concurrency = health.max_concurrency
    audit.record(
        session,
        operator,
        "external_agent.profile.health_passed",
        "external_agent_profile",
        None,
        after={
            "slug": slug,
            "revision": profile.revision,
            "capabilities": health.capabilities,
            "enforced_limits": health.enforced_limits,
            "remote_max_concurrency": health.max_concurrency,
        },
    )
    session.flush()
    return profile, health


def submit_task(
    session: Session, operator: Operator, slug: str, request: TaskRequest
) -> ExternalAgentTask:
    workload = _lock_workload(session)
    profile = _get_profile(session, slug, lock=True)
    now = datetime.now(UTC)
    schedule = WorkloadSchedule.model_validate(workload.schedule)
    if get_settings().external_agents_deny:
        raise ExternalAgentError("External-agent tasks are blocked by the deployment deny switch.")
    if not workload.enabled:
        raise ExternalAgentError("External-agent workload is switched off.")
    if not schedule.allows(now):
        raise ExternalAgentError(
            "Outside the configured external-agent window; next open: "
            f"{schedule.next_open(now).isoformat()}."
        )
    if not profile.enabled:
        raise ExternalAgentError("This external-agent profile is switched off.")
    if not _check_healthy(profile, now):
        raise ExternalAgentError("Run a successful health check for this profile first.")
    if request.purpose not in profile.allowed_purposes:
        raise ExternalAgentError("This profile is not approved for that purpose.")
    if request.purpose not in profile.health_capabilities:
        raise ExternalAgentError("The tested remote service does not support that purpose.")
    if _active_count(session) >= schedule.max_concurrency:
        raise ExternalAgentError("The external-agent workload has reached its concurrency limit.")
    if _active_count(session, slug) >= min(
        profile.max_concurrency, profile.health_max_concurrency or 0
    ):
        raise ExternalAgentError("This external-agent profile has reached its concurrency limit.")
    day_ago = now - timedelta(hours=24)
    recent_global = int(
        session.scalar(
            select(func.count())
            .select_from(ExternalAgentTask)
            .where(ExternalAgentTask.created_at >= day_ago)
        )
        or 0
    )
    recent_profile = int(
        session.scalar(
            select(func.count())
            .select_from(ExternalAgentTask)
            .where(
                ExternalAgentTask.profile_slug == slug,
                ExternalAgentTask.created_at >= day_ago,
            )
        )
        or 0
    )
    if recent_global >= schedule.max_items_per_run:
        raise ExternalAgentError(
            "External-agent rolling task cap reached "
            f"({recent_global}/{schedule.max_items_per_run})."
        )
    if recent_profile >= profile.max_tasks_per_day:
        raise ExternalAgentError(
            f"Profile rolling task cap reached ({recent_profile}/{profile.max_tasks_per_day})."
        )
    reserved = session.scalar(
        select(func.coalesce(func.sum(ExternalAgentTask.reserved_cost_usd), 0)).where(
            ExternalAgentTask.profile_slug == slug,
            (
                (ExternalAgentTask.created_at >= day_ago)
                & (
                    ExternalAgentTask.status.in_(
                        ("pending", "admitted", "running", "cancellation_requested")
                    )
                    | ExternalAgentTask.external_task_id.is_not(None)
                    | (ExternalAgentTask.status == "outcome_unknown")
                )
            )
            | (ExternalAgentTask.status == "outcome_unknown"),
        )
    )
    if Decimal(str(reserved or 0)) + profile.max_cost_per_task_usd > profile.max_spend_per_day_usd:
        raise ExternalAgentError("This profile's reserved rolling 24-hour spend cap is exhausted.")

    task = ExternalAgentTask(
        profile_slug=slug,
        requested_by_operator_id=operator.id,
        purpose=request.purpose,
        objective=request.objective,
        idempotency_key=f"africasignal:external-agent:{slug}:{uuid4()}",
        profile_revision=profile.revision,
        workload_revision=workload.revision,
        reserved_cost_usd=profile.max_cost_per_task_usd,
        status="pending",
        deadline_at=min(
            now + timedelta(seconds=profile.timeout_seconds),
            schedule.current_window_end(now) or now,
        ),
        retention_until=now + timedelta(days=30),
    )
    session.add(task)
    session.flush()
    queue.enqueue(
        session,
        "external_agent_submit",
        {"task_id": task.id},
        dedupe_key=f"external-agent:submit:{task.id}",
    )
    audit.record(
        session,
        operator,
        "external_agent.task.submit",
        "external_agent_task",
        task.id,
        after={
            "profile": slug,
            "purpose": request.purpose,
            "profile_revision": profile.revision,
            "workload_revision": workload.revision,
            "reserved_cost_usd": str(task.reserved_cost_usd),
            "idempotency_key": task.idempotency_key,
        },
    )
    return task


def cancel_task(session: Session, operator: Operator, task_id: int) -> ExternalAgentTask:
    _lock_workload(session)
    task = session.scalar(
        select(ExternalAgentTask).where(ExternalAgentTask.id == task_id).with_for_update()
    )
    if task is None:
        raise ExternalAgentError("No such external-agent task.")
    before = task.status
    now = datetime.now(UTC)
    if task.status in ("pending", "admitted"):
        task.status = "cancelled"
        task.finished_at = now
    elif task.status in ("running", "cancellation_requested"):
        task.status = "cancellation_requested"
        kind = "external_agent_cancel" if task.external_task_id else "external_agent_reconcile"
        queue.enqueue(
            session,
            kind,
            {"task_id": task.id},
            dedupe_key=f"external-agent:{kind}:{task.id}:operator:{int(now.timestamp())}",
        )
    elif task.status == "outcome_unknown":
        task.status = "cancellation_requested"
        queue.enqueue(
            session,
            "external_agent_reconcile",
            {"task_id": task.id},
            dedupe_key=f"external-agent:reconcile:{task.id}:operator:{int(now.timestamp())}",
        )
    else:
        raise ExternalAgentError(f"Task is already {task.status}.")
    audit.record(
        session,
        operator,
        "external_agent.task.cancel",
        "external_agent_task",
        task.id,
        before={"status": before},
        after={"status": task.status},
    )
    return task


def recent_tasks(session: Session, limit: int = 100) -> list[ExternalAgentTask]:
    return list(
        session.scalars(
            select(ExternalAgentTask).order_by(ExternalAgentTask.created_at.desc()).limit(limit)
        ).all()
    )


def task_json(task: ExternalAgentTask) -> dict[str, object]:
    return {
        "id": task.id,
        "profile_slug": task.profile_slug,
        "purpose": task.purpose,
        "objective": task.objective,
        "idempotency_key": task.idempotency_key,
        "status": task.status,
        "external_task_id": task.external_task_id,
        "result_text": task.result_text,
        "reported_usage": (
            {"source": "unverified_remote_report", **task.reported_usage}
            if task.reported_usage
            else None
        ),
        "reserved_cost_usd": str(task.reserved_cost_usd),
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "deadline_at": task.deadline_at.isoformat(),
        "last_error": task.last_error,
        "profile_revision": task.profile_revision,
        "workload_revision": task.workload_revision,
    }
