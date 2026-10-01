"""``notify_followers``: notifications and correction emails for a newly published version."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.hooks import register_publication_hook
from africasignal.publish.notify import notify_followers as notify
from africasignal.publish.notify import queue_notifications

log = logging.getLogger("africasignal.notify_followers")


@register("notify_followers")
def notify_followers(ctx: JobContext) -> None:
    payload = ctx.job.payload
    result = notify(
        ctx.session, int(payload["version_id"]), datetime.now(UTC), kind=payload.get("kind")
    )
    log.info("notify_followers: %s", result, extra={"job_id": ctx.job.id})


# Publishing a version queues this job for its followers. Registered here because the worker and
# scheduler import every handler at startup (``load_all``), and they are the processes that publish.
register_publication_hook(queue_notifications)
