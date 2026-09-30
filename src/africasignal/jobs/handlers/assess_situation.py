"""``assess_situation``: recompute one situation's assessment (spec B8, AS-011)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.situations import assess_situation as assess

log = logging.getLogger("africasignal.assess_situation")


@register("assess_situation")
def assess_situation(ctx: JobContext) -> None:
    outcome = assess(ctx.session, int(ctx.job.payload["situation_id"]), datetime.now(UTC))
    log.info(
        "assess_situation %s: %s",
        ctx.job.payload["situation_id"],
        outcome.outcome,
        extra={"job_id": ctx.job.id, "reason": outcome.reason},
    )
