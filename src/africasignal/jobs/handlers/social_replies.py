"""Account-bound X mention intake and reviewed reply dispatch."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.operations import social_replies

log = logging.getLogger("africasignal.social_reply_jobs")


@register("social_mention_poll")
def social_mention_poll(ctx: JobContext) -> None:
    result = social_replies.poll_mentions(
        ctx.session, datetime.now(UTC), force=bool(ctx.job.payload.get("force", False))
    )
    if any(result.__dict__.values()):
        log.info("social_mention_poll: %s", result, extra={"job_id": ctx.job.id})


@register("social_reply_dispatch")
def social_reply_dispatch(ctx: JobContext) -> None:
    result = social_replies.dispatch_pending_replies(ctx.session, datetime.now(UTC))
    if any(result.__dict__.values()):
        log.info("social_reply_dispatch: %s", result, extra={"job_id": ctx.job.id})


@register("social_mention_expire")
def social_mention_expire(ctx: JobContext) -> None:
    result = social_replies.expire(ctx.session, datetime.now(UTC))
    if any(result.values()):
        log.info("social_mention_expire: %s", result, extra={"job_id": ctx.job.id})
