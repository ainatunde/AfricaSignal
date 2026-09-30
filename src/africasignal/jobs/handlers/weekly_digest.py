"""``weekly_digest``: queue this week's digest emails. Scheduled Mondays 07:00 Africa/Lagos."""

from __future__ import annotations

from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.digest import run_weekly_digest


@register("weekly_digest")
def weekly_digest(ctx: JobContext) -> None:
    run_weekly_digest(ctx.session, datetime.now(UTC))
