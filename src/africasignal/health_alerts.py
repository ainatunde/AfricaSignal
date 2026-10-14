"""Alerts on sources, jobs and the model budget (AS-041).

The scheduler's ``check_health`` job (every 15 minutes) calls :func:`check`, which keeps operational
alerts in the same ``ops.alert.<code>`` rows, audit rows and log lines as the backup alerts (see
``africasignal.backup_alerts``) and so shows on the console's Alerts page:

- ``sources_failing``: at least one active source is marked failing (``fetch_source`` marks a
  source failing after repeated fetch failures). Clears when none is.
- ``jobs_dead``: at least one job died (used up its attempts) in the last 24 hours. Clears when
  none has died for a day or when the operator retries them (Jobs page).
- ``llm_budget_80``: today's model spend is at 80 percent of the daily budget (Lagos day) or more.
  Clears at the start of the next budget day or when the limit is raised.

Nothing is sent anywhere: there is no operator notification channel yet, so these are seen in the
console, the audit log and the log. Details never hold addresses or payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import backup_alerts
from africasignal.backup_alerts import CheckResult, Finding
from africasignal.config import get_settings
from africasignal.jobs.retention import COMPLETED_JOB_RETENTION_DAYS
from africasignal.models import (
    Job,
    Measurement,
    Outbox,
    RateLimitState,
    Series,
    Setting,
    SocialPublication,
    Source,
)
from africasignal.operations import costs

SOURCES_FAILING = "sources_failing"
JOBS_DEAD = "jobs_dead"
LLM_BUDGET_80 = "llm_budget_80"
RETENTION_BACKLOG = "retention_backlog"
SCHEDULER_STALLED = "scheduler_stalled"
QUEUE_LAG = "queue_lag"
SERIES_REFERENCE_STALE = "series_reference_stale"
DELIVERY_LAG = "delivery_lag"
EMAIL_OUTCOME_UNKNOWN = "email_outcome_unknown"
SOCIAL_OUTCOME_UNKNOWN = "social_outcome_unknown"
STORAGE_UNAVAILABLE = "storage_unavailable"
ALERT_CODES = (
    SOURCES_FAILING,
    JOBS_DEAD,
    LLM_BUDGET_80,
    RETENTION_BACKLOG,
    SCHEDULER_STALLED,
    QUEUE_LAG,
    SERIES_REFERENCE_STALE,
    DELIVERY_LAG,
    EMAIL_OUTCOME_UNKNOWN,
    SOCIAL_OUTCOME_UNKNOWN,
    STORAGE_UNAVAILABLE,
)
DEAD_JOB_WINDOW = timedelta(hours=24)
ERROR_LIMIT = 200
SCHEDULER_TICK_KEY = "ops.scheduler_last_tick"
SCHEDULER_STALE_AFTER = timedelta(minutes=5)
QUEUE_LAG_AFTER = timedelta(minutes=5)
DELIVERY_LAG_AFTER = timedelta(minutes=10)
SOCIAL_UNKNOWN_AFTER = timedelta(minutes=5)
STORAGE_PROBE_STALE_AFTER = timedelta(minutes=12)
# This follows the public assessment freshness rule. It is a reference-period age, not a claim
# that a successful fetch means a series has been updated.
SERIES_REFERENCE_STALE_AFTER = timedelta(days=120)


def evaluate(session: Session, now: datetime) -> list[Finding]:
    """What is wrong right now. Reads only."""
    findings: list[Finding] = []

    failing = list(
        session.scalars(
            select(Source)
            .where(Source.active.is_(True), Source.health == "failing")
            .order_by(Source.slug)
        )
    )
    if failing:
        findings.append(
            Finding(
                SOURCES_FAILING,
                f"{len(failing)} active source{'s are' if len(failing) != 1 else ' is'} failing: "
                + ", ".join(s.slug for s in failing[:10])
                + (" and more" if len(failing) > 10 else ""),
                {
                    "sources": [
                        {
                            "slug": s.slug,
                            "consecutive_failures": s.consecutive_failures,
                            "last_success_at": s.last_success_at.isoformat()
                            if s.last_success_at
                            else None,
                            "last_error": (s.last_error or "")[:ERROR_LIMIT] or None,
                        }
                        for s in failing[:10]
                    ]
                },
            )
        )

    dead = session.execute(
        select(Job.kind, func.count())
        .where(Job.status == "dead", Job.finished_at >= now - DEAD_JOB_WINDOW)
        .group_by(Job.kind)
        .order_by(Job.kind)
    ).all()
    if dead:
        total = sum(n for _, n in dead)
        findings.append(
            Finding(
                JOBS_DEAD,
                f"{total} job{'s' if total != 1 else ''} died in the last 24 hours "
                f"({', '.join(f'{k} {n}' for k, n in dead)}). See the Jobs page.",
                {"by_kind": {k: n for k, n in dead}, "window_hours": 24},
            )
        )

    scheduler = session.scalar(select(Setting.value).where(Setting.key == SCHEDULER_TICK_KEY))
    scheduler_tick = (
        _parse_time(scheduler.get("last_tick_at")) if isinstance(scheduler, dict) else None
    )
    if scheduler_tick is not None and now - scheduler_tick > SCHEDULER_STALE_AFTER:
        age_seconds = int((now - scheduler_tick).total_seconds())
        findings.append(
            Finding(
                SCHEDULER_STALLED,
                f"The last committed scheduler tick was {age_seconds // 60} minutes ago.",
                {"last_tick_at": scheduler_tick.isoformat(), "age_seconds": age_seconds},
            )
        )

    oldest_queued = session.execute(
        select(Job.id, Job.kind, Job.created_at, Job.run_at)
        .where(Job.status == "queued", Job.run_at <= now, Job.created_at <= now - QUEUE_LAG_AFTER)
        .order_by(Job.created_at, Job.id)
        .limit(1)
    ).first()
    if oldest_queued is not None:
        job_id, kind, created_at, run_at = oldest_queued
        findings.append(
            Finding(
                QUEUE_LAG,
                f"Job #{job_id} ({kind}) has waited more than "
                f"{int(QUEUE_LAG_AFTER.total_seconds() // 60)} minutes.",
                {
                    "job_id": job_id,
                    "kind": kind,
                    "created_at": created_at.isoformat(),
                    "run_at": run_at.isoformat(),
                    "age_seconds": int((now - created_at).total_seconds()),
                },
            )
        )

    latest_periods = (
        select(
            Measurement.series_id.label("series_id"),
            func.max(Measurement.period_end).label("period_end"),
        )
        .where(Measurement.superseded_by_id.is_(None))
        .group_by(Measurement.series_id)
        .subquery()
    )
    stale_series: list[dict[str, object]] = []
    for item_code, source_slug, period_end in session.execute(
        select(Series.item_code, Source.slug, latest_periods.c.period_end)
        .join(Source, Source.id == Series.source_id)
        .outerjoin(latest_periods, latest_periods.c.series_id == Series.id)
        .where(Source.active.is_(True), Series.frequency == "monthly")
        .order_by(Source.slug, Series.item_code)
    ):
        age_days = (now.date() - period_end).days if period_end is not None else None
        if age_days is None or age_days > SERIES_REFERENCE_STALE_AFTER.days:
            stale_series.append(
                {
                    "source": source_slug,
                    "item_code": item_code,
                    "latest_period_end": period_end.isoformat() if period_end is not None else None,
                    "age_days": age_days,
                }
            )
    if stale_series:
        findings.append(
            Finding(
                SERIES_REFERENCE_STALE,
                f"{len(stale_series)} active monthly series have no usable measurement "
                f"or a reference period older than {SERIES_REFERENCE_STALE_AFTER.days} days.",
                {
                    "threshold_days": SERIES_REFERENCE_STALE_AFTER.days,
                    "series_count": len(stale_series),
                    "series": stale_series[:20],
                },
            )
        )

    lagging_delivery = session.execute(
        select(Outbox.id, Outbox.kind, Outbox.created_at, Outbox.status)
        .where(
            Outbox.kind.in_(("email_digest", "email_correction", "email_insight")),
            Outbox.status.in_(("pending", "failed")),
            Outbox.next_attempt_at <= now,
            Outbox.created_at <= now - DELIVERY_LAG_AFTER,
        )
        .order_by(Outbox.created_at, Outbox.id)
        .limit(1)
    ).first()
    if lagging_delivery is not None:
        outbox_id, kind, created_at, status = lagging_delivery
        findings.append(
            Finding(
                DELIVERY_LAG,
                f"Email delivery {outbox_id} ({kind}) has been due for at least "
                f"{int(DELIVERY_LAG_AFTER.total_seconds() // 60)} minutes.",
                {
                    "outbox_id": outbox_id,
                    "kind": kind,
                    "status": status,
                    "created_at": created_at.isoformat(),
                    "age_seconds": int((now - created_at).total_seconds()),
                },
            )
        )

    unknown_email = session.execute(
        select(Outbox.id, Outbox.kind, Outbox.status, Outbox.attempt_started_at)
        .where(
            (Outbox.status == "outcome_unknown")
            | (
                (Outbox.status == "sending")
                & (Outbox.attempt_started_at <= now - SOCIAL_UNKNOWN_AFTER)
            )
        )
        .order_by(Outbox.id)
        .limit(20)
    ).all()
    if unknown_email:
        findings.append(
            Finding(
                EMAIL_OUTCOME_UNKNOWN,
                f"{len(unknown_email)} email outcome(s) need reconciliation; do not blindly retry.",
                {
                    "outbox_rows": [
                        {
                            "outbox_id": outbox_id,
                            "kind": kind,
                            "status": status,
                            "attempt_started_at": attempted_at.isoformat()
                            if attempted_at
                            else None,
                        }
                        for outbox_id, kind, status, attempted_at in unknown_email
                    ]
                },
            )
        )

    unknown_social = session.execute(
        select(
            SocialPublication.id,
            SocialPublication.channel,
            SocialPublication.status,
            SocialPublication.attempt_started_at,
        )
        .where(
            (SocialPublication.status == "outcome_unknown")
            | (
                (SocialPublication.status == "sending")
                & (SocialPublication.attempt_started_at <= now - SOCIAL_UNKNOWN_AFTER)
            )
        )
        .order_by(SocialPublication.id)
        .limit(20)
    ).all()
    if unknown_social:
        findings.append(
            Finding(
                SOCIAL_OUTCOME_UNKNOWN,
                f"{len(unknown_social)} social publication outcome(s) need reconciliation; "
                "do not blindly retry them.",
                {
                    "publications": [
                        {
                            "publication_id": publication_id,
                            "channel": channel,
                            "status": status,
                            "attempt_started_at": attempt_started_at.isoformat()
                            if attempt_started_at
                            else None,
                        }
                        for publication_id, channel, status, attempt_started_at in unknown_social
                    ]
                },
            )
        )

    storage_status_value = session.scalar(
        select(Setting.value).where(Setting.key == "ops.storage_probe")
    )
    storage_status: dict[str, object] = (
        storage_status_value if isinstance(storage_status_value, dict) else {}
    )
    status_is_dict = isinstance(storage_status_value, dict)
    checked_at = _parse_time(storage_status.get("checked_at"))
    configured = storage_status.get("configured") is True
    available = storage_status.get("available") is True
    production = get_settings().env != "development"
    storage_problem = (
        (not status_is_dict and production)
        or (not configured and production)
        or (configured and not available)
        or (production and checked_at is None)
        or (production and checked_at is not None and now - checked_at > STORAGE_PROBE_STALE_AFTER)
    )
    if storage_problem:
        findings.append(
            Finding(
                STORAGE_UNAVAILABLE,
                "Evidence object storage is unavailable, unconfigured, or its last probe is stale.",
                {
                    "configured": configured,
                    "available": available,
                    "checked_at": checked_at.isoformat() if checked_at else None,
                    "error_type": storage_status.get("error_type") if status_is_dict else None,
                    "stale_after_seconds": int(STORAGE_PROBE_STALE_AFTER.total_seconds()),
                },
            )
        )

    budget = costs.today(session, now)
    if budget.warn:
        findings.append(
            Finding(
                LLM_BUDGET_80,
                "Model budget use today is "
                f"${budget.committed_and_reserved:.2f} of ${budget.limit:.2f} "
                f"({budget.fraction:.0%}). New model work waits for the next day once it is spent.",
                {
                    "spent_usd": f"{budget.spent:.4f}",
                    "reserved_usd": f"{budget.reserved:.4f}",
                    "limit_usd": f"{budget.limit:.2f}",
                },
            )
        )
    old_job = session.scalar(
        select(Job.id)
        .where(
            Job.status == "done",
            Job.finished_at < now - timedelta(days=COMPLETED_JOB_RETENTION_DAYS + 1),
        )
        .limit(1)
    )
    old_limit = session.scalar(
        select(RateLimitState.key_hash)
        .where(RateLimitState.expires_at < now - timedelta(hours=1))
        .limit(1)
    )
    if old_job is not None or old_limit is not None:
        findings.append(
            Finding(
                RETENTION_BACKLOG,
                "Operational retention is behind; inspect archive jobs "
                "and unresolved provider reservations.",
                {
                    "completed_jobs_overdue": old_job is not None,
                    "expired_limits_overdue": old_limit is not None,
                },
            )
        )
    return findings


def check(session: Session, now: datetime | None = None) -> CheckResult:
    """Open, update and resolve the operational alerts. The caller commits."""
    now = now or datetime.now(UTC)
    return backup_alerts.apply(session, now, ALERT_CODES, evaluate(session, now))


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def check_storage_probe(session: Session, now: datetime | None = None) -> dict[str, object]:
    """Persist the latest bounded object-store probe without exposing credentials or error text."""
    from africasignal.storage import StorageNotConfigured, probe_storage

    now = now or datetime.now(UTC)
    status: dict[str, object] = {
        "checked_at": now.isoformat(),
        "configured": True,
        "available": True,
    }
    try:
        probe_storage(session)
    except StorageNotConfigured:
        status.update(configured=False, available=False, error_type="not_configured")
    except Exception as exc:
        status.update(available=False, error_type=type(exc).__name__)
    backup_alerts._write(session, "ops.storage_probe", status)
    return status
