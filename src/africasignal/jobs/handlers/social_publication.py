"""Dispatch operator-approved X posts once per durable approval record."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.social import dispatch_pending

log = logging.getLogger("africasignal.dispatch_social_publication")


@register("dispatch_social_publication")
def dispatch_social_publication(ctx: JobContext) -> None:
    result = dispatch_pending(ctx.session, datetime.now(UTC))
    if result.sent or result.rejected or result.unknown or result.cancelled or result.held:
        log.info("dispatch_social_publication: %s", result, extra={"job_id": ctx.job.id})
