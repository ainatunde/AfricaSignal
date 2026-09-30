"""``check_backups``: hourly, raise or resolve the backup and restore-drill alerts (AS-041)."""

from __future__ import annotations

from africasignal import backup_alerts
from africasignal.jobs.handlers import JobContext, register


@register("check_backups")
def check_backups(ctx: JobContext) -> None:
    backup_alerts.check(ctx.session)
