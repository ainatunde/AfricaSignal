"""Refresh private, deterministic, evidence-linked editorial opportunities."""

from __future__ import annotations

import logging

from africasignal.assess.insight_candidates import refresh_and_generate
from africasignal.jobs.handlers import JobContext, register

log = logging.getLogger("africasignal.insight_candidates")


@register("refresh_insight_candidates")
def refresh_insight_candidates(ctx: JobContext) -> None:
    result = refresh_and_generate(ctx.session)
    if result["created"] or result["marked_stale"]:
        log.info("insight candidates refreshed", extra={**result, "job_id": ctx.job.id})
