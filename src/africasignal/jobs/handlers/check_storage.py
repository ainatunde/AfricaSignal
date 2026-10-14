"""``check_storage``: every five minutes, make a bounded read-only bucket availability probe."""

from __future__ import annotations

from africasignal import health_alerts
from africasignal.jobs.handlers import JobContext, register


@register("check_storage")
def check_storage(ctx: JobContext) -> None:
    health_alerts.check_storage_probe(ctx.session)
