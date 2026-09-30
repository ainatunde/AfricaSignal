"""``apply_retention`` (daily) and ``mirror_deletions`` (queued when an account is deleted):
see ``publish/retention.py`` and ``publish/deletions.py``."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime

from africasignal.jobs.handlers import JobContext, register
from africasignal.publish import deletions, retention
from africasignal.storage import store_for_session

log = logging.getLogger("africasignal.retention")


@register("apply_retention")
def apply_retention(ctx: JobContext) -> None:
    result = retention.run(ctx.session, datetime.now(UTC), store_for_session(ctx.session))
    counts = {k: v for k, v in asdict(result).items() if v}
    if counts:
        log.info("retention: %s", counts, extra={"job_id": ctx.job.id})


@register(deletions.MIRROR_JOB)
def mirror_deletions(ctx: JobContext) -> None:
    store = store_for_session(ctx.session)
    if store is None:
        log.warning(
            "deletion ledger not copied to object storage: no bucket is configured",
            extra={"job_id": ctx.job.id},
        )
        return
    deletions.mirror_pending(ctx.session, store, datetime.now(UTC))
