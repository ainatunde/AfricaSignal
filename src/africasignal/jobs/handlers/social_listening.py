"""Scheduled X Recent Search and mandatory ID-only lead retention cleanup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.operations import social_listening

log = logging.getLogger("africasignal.social_listening_jobs")


@register("social_listen_poll")
def social_listen_poll(ctx: JobContext) -> None:
    result = social_listening.poll_queries(
        ctx.session,
        datetime.now(UTC),
        force=bool(ctx.job.payload.get("force", False)),
        operator_id=(
            int(ctx.job.payload["operator_id"])
            if isinstance(ctx.job.payload.get("operator_id"), int)
            else None
        ),
    )
    if any(result.__dict__.values()):
        log.info("social_listen_poll: %s", result, extra={"job_id": ctx.job.id})


@register("social_listen_expire")
def social_listen_expire(ctx: JobContext) -> None:
    result = social_listening.expire(ctx.session, datetime.now(UTC))
    if any(result.values()):
        log.info("social_listen_expire: %s", result, extra={"job_id": ctx.job.id})
