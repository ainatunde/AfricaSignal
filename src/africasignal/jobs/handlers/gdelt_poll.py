"""``gdelt_poll``: read the GDELT windows published since the last poll (spec B6.7)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.sources import gdelt
from africasignal.sources.health import record_failure
from africasignal.sources.permissions import current_permission

log = logging.getLogger("africasignal.gdelt_poll")


@register("gdelt_poll")
def gdelt_poll(ctx: JobContext) -> None:
    session = ctx.session
    source = gdelt.gdelt_source(session)
    if source is None:
        log.info("skipping gdelt_poll: no active GDELT source")
        return
    permission = current_permission(session, source.id)
    if permission is None or not permission.may_collect:
        log.warning(
            "refusing to poll GDELT: source %s has no approved permission to collect", source.slug
        )
        return
    try:
        result = gdelt.poll(session, source)
    except Exception as exc:
        record_failure(session, source.id, f"{type(exc).__name__}: {exc}")
        raise
    # Stopping early on a window GDELT has not published yet is normal, not a failure.
    source.last_success_at = datetime.now(UTC)
    source.consecutive_failures = 0
    source.last_error = None
    source.health = "healthy"
    log.info(
        "gdelt poll: newest window %s, %d processed, %d skipped%s",
        result.latest,
        len(result.windows),
        len(result.skipped),
        f", stopped at {result.stopped_early}" if result.stopped_early else "",
        extra={"job_id": ctx.job.id},
    )
