"""``assess_situation``: recompute one situation's assessment and apply the publication policy
(spec B8, AS-011, AS-012)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.situations import assess_situation as assess
from africasignal.publish.versions import apply_policy

log = logging.getLogger("africasignal.assess_situation")


@register("assess_situation")
def assess_situation(ctx: JobContext) -> None:
    now = datetime.now(UTC)
    outcome = assess(ctx.session, int(ctx.job.payload["situation_id"]), now)
    decision = None
    if outcome.outcome == "created" and outcome.version is not None:
        decision = apply_policy(ctx.session, outcome.version.id, now)
    log.info(
        "assess_situation %s: %s",
        ctx.job.payload["situation_id"],
        outcome.outcome,
        extra={
            "job_id": ctx.job.id,
            "reason": outcome.reason,
            "decision": decision.status if decision else None,
            "rules": list(decision.reasons) if decision else [],
        },
    )
