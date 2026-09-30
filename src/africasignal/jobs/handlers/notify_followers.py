"""``notify_followers``: notifications and correction emails for a newly published version."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.notify import notify_followers as notify

log = logging.getLogger("africasignal.notify_followers")


@register("notify_followers")
def notify_followers(ctx: JobContext) -> None:
    payload = ctx.job.payload
    result = notify(
        ctx.session, int(payload["version_id"]), datetime.now(UTC), kind=payload.get("kind")
    )
    log.info("notify_followers: %s", result, extra={"job_id": ctx.job.id})
