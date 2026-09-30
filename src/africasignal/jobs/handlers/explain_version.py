"""``explain_version`` and ``explain_backfill``: write the "Why this matters" paragraph of an
assessment version (spec B8.4, AS-028).

``assess_situation`` queues ``explain_version`` for each version it stores, after the publication
policy has decided. The page is live with its facts first and gains the paragraph when the job
has run, and a failed or missing explanation never holds anything back.

``explain_backfill`` runs on the scheduler's hourly slot and catches up on versions that were not
explained because no API key was set. It does nothing until a key exists. A day's budget that runs
out defers the work to the next budget day instead of failing it.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.assess.explain import (
    PROMPT_VERSION,
    PURPOSE,
    explain_version,
    versions_missing_explanation,
)
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext, register
from africasignal.llm import BudgetExhausted, build_adapter
from africasignal.llm.budget import defer_until_next_day

log = logging.getLogger("africasignal.explain_version")

BACKFILL_BATCH = 10


def enqueue_explanation(session: Session, version_id: int) -> int | None:
    """Queue the explanation of a version, once per prompt version."""
    return queue.enqueue(
        session,
        "explain_version",
        {"version_id": version_id},
        dedupe_key=f"explain_version:{version_id}:{PROMPT_VERSION}",
    )


def _has_api_key(ctx: JobContext) -> bool:
    return bool(settings_store.get(ctx.session, "anthropic_api_key"))


@register("explain_version")
def explain_version_job(ctx: JobContext) -> None:
    version_id = int(ctx.job.payload["version_id"])
    if not _has_api_key(ctx):
        log.info(
            "no API key: version %s left for the backfill", version_id, extra={"job_id": ctx.job.id}
        )
        return
    try:
        outcome = explain_version(
            ctx.session, build_adapter(ctx.session), version_id, job_id=ctx.job.id
        )
    except BudgetExhausted as exc:
        defer_until_next_day(
            ctx.session,
            "explain_version",
            ctx.job.payload,
            dedupe_key=f"explain_version:{version_id}:budget",
            retry_at=exc.retry_at,
        )
        log.info(
            "daily %s budget spent: version %s deferred to %s",
            PURPOSE,
            version_id,
            exc.retry_at.isoformat(),
            extra={"job_id": ctx.job.id},
        )
        return
    log.info("version %s: %s", version_id, outcome, extra={"job_id": ctx.job.id})


@register("explain_backfill")
def explain_backfill_job(ctx: JobContext) -> None:
    if not _has_api_key(ctx):
        return
    adapter = build_adapter(ctx.session)
    done = 0
    try:
        for version_id in versions_missing_explanation(ctx.session, BACKFILL_BATCH):
            explain_version(ctx.session, adapter, version_id, job_id=ctx.job.id)
            ctx.session.commit()
            done += 1
    except BudgetExhausted:
        log.info("daily %s budget spent: backfill stops", PURPOSE, extra={"job_id": ctx.job.id})
    if done:
        log.info("backfill tried %d versions", done, extra={"job_id": ctx.job.id})
