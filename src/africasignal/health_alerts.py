"""Alerts on sources, jobs and the model budget (AS-041).

The scheduler's ``check_health`` job (every 15 minutes) calls :func:`check`, which keeps three
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
from africasignal.models import Job, Source
from africasignal.operations import costs

SOURCES_FAILING = "sources_failing"
JOBS_DEAD = "jobs_dead"
LLM_BUDGET_80 = "llm_budget_80"
ALERT_CODES = (SOURCES_FAILING, JOBS_DEAD, LLM_BUDGET_80)
DEAD_JOB_WINDOW = timedelta(hours=24)
ERROR_LIMIT = 200


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

    budget = costs.today(session, now)
    if budget.warn:
        findings.append(
            Finding(
                LLM_BUDGET_80,
                f"Model spend today is ${budget.spent:.2f} of ${budget.limit:.2f} "
                f"({budget.fraction:.0%}). New model work waits for the next day once it is spent.",
                {"spent_usd": f"{budget.spent:.4f}", "limit_usd": f"{budget.limit:.2f}"},
            )
        )
    return findings


def check(session: Session, now: datetime | None = None) -> CheckResult:
    """Open, update and resolve the three alerts. The caller commits."""
    now = now or datetime.now(UTC)
    return backup_alerts.apply(session, now, ALERT_CODES, evaluate(session, now))
