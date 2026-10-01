"""``dispatch_outbox``: send pending outbox emails (spec B4). Runs every minute."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.outbox import dispatch_with_configured_provider

log = logging.getLogger("africasignal.dispatch_outbox")


@register("dispatch_outbox")
def dispatch_outbox(ctx: JobContext) -> None:
    result = dispatch_with_configured_provider(ctx.session, datetime.now(UTC))
    if result and (result.sent or result.retried or result.dead or result.held):
        log.info("dispatch_outbox: %s", result, extra={"job_id": ctx.job.id})
