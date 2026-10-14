"""``check_health``: every minute, raise or resolve deterministic operating incidents."""

from __future__ import annotations

from africasignal import health_alerts
from africasignal.guardian import remediation
from africasignal.jobs.handlers import JobContext, register


@register("check_health")
def check_health(ctx: JobContext) -> None:
    health_alerts.check(ctx.session)
    remediation.check_and_repair(ctx.session)
