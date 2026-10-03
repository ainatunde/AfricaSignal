"""Minute-by-minute, bounded operational retention."""

from africasignal.jobs.execution import checkpoint
from africasignal.jobs.handlers import JobContext, register
from africasignal.jobs.retention import archive_completed, prune_limits


@register("archive_operations")
def archive_operations(ctx: JobContext) -> None:
    checkpoint()
    archive_completed(ctx.session)
    checkpoint()
    prune_limits(ctx.session)
