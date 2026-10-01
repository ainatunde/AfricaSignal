"""``prune_events``: delete product events past their 13-month retention (plan B3.8). Daily."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal import metrics
from africasignal.jobs.handlers import JobContext, register

log = logging.getLogger("africasignal.prune_events")


@register("prune_events")
def prune_events(ctx: JobContext) -> None:
    deleted = metrics.prune_events(ctx.session, datetime.now(UTC))
    if deleted:
        log.info("pruned %d events", deleted, extra={"job_id": ctx.job.id})
