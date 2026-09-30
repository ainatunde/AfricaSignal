"""``assess_situation``: recompute one situation's assessment and apply the publication policy
(spec B8, B9, AS-011, AS-012, AS-013).

A payload with ``correction`` comes from an invalidation: the new version carries that sentence,
is published as a correction, and if nothing valid is left to assess the current version is
replaced by a withdrawn one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.models import Situation
from africasignal.publish.invalidation import withdraw_situation
from africasignal.publish.situations import assess_situation as assess
from africasignal.publish.versions import apply_policy

log = logging.getLogger("africasignal.assess_situation")


@register("assess_situation")
def assess_situation(ctx: JobContext) -> None:
    now = datetime.now(UTC)
    situation_id = int(ctx.job.payload["situation_id"])
    correction = ctx.job.payload.get("correction")
    outcome = assess(ctx.session, situation_id, now, correction=correction)
    decision = None
    if outcome.outcome == "created" and outcome.version is not None:
        decision = apply_policy(
            ctx.session,
            outcome.version.id,
            now,
            kind="correction" if correction else "new_version",
        )
    elif outcome.outcome == "skipped" and correction:
        situation = ctx.session.get(Situation, situation_id)
        if situation is not None and withdraw_situation(ctx.session, situation, now):
            log.info("assess_situation %s: withdrawn", situation_id, extra={"job_id": ctx.job.id})
    log.info(
        "assess_situation %s: %s",
        situation_id,
        outcome.outcome,
        extra={
            "job_id": ctx.job.id,
            "reason": outcome.reason,
            "decision": decision.status if decision else None,
            "rules": list(decision.reasons) if decision else [],
        },
    )
