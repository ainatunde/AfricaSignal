"""``check_public_pages``: bounded checks of the live reader-facing HTTP pages."""

from __future__ import annotations

from africasignal.guardian.public_pages import check_public_pages as run_check
from africasignal.jobs.handlers import JobContext, register


@register("check_public_pages")
def check_public_pages(ctx: JobContext) -> None:
    run_check(ctx.session)
