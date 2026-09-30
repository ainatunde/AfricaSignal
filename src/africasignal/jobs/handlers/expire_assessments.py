"""``expire_assessments``: hourly, mark assessments past ``valid_until`` as stale."""

from __future__ import annotations

from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.expiry import expire_assessments as run_expiry


@register("expire_assessments")
def expire_assessments(ctx: JobContext) -> None:
    run_expiry(ctx.session, datetime.now(UTC))
