"""``check_health``: every 15 minutes, raise or resolve the source, dead-job and model-budget
alerts (AS-041)."""

from __future__ import annotations

from africasignal import health_alerts
from africasignal.jobs.handlers import JobContext, register


@register("check_health")
def check_health(ctx: JobContext) -> None:
    health_alerts.check(ctx.session)
