"""``invalidate``: act on inputs that are no longer valid (spec B9, AS-013)."""

from __future__ import annotations

from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.invalidation import invalidate as run_invalidation


@register("invalidate")
def invalidate(ctx: JobContext) -> None:
    payload = ctx.job.payload
    run_invalidation(
        ctx.session, str(payload["kind"]), [int(i) for i in payload["ids"]], datetime.now(UTC)
    )
