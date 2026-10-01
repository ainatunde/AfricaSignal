"""``release_held_versions``: publish first high-severity versions whose hold has ended
(publication rule R7)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish.versions import release_held

log = logging.getLogger("africasignal.release_held_versions")


@register("release_held_versions")
def release_held_versions(ctx: JobContext) -> None:
    released = release_held(ctx.session, datetime.now(UTC))
    if released:
        log.info("released %d held versions", len(released), extra={"job_id": ctx.job.id})
