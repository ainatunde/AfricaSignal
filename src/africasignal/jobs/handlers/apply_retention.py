"""``apply_retention`` (daily) and ``mirror_deletions`` (queued when an account is deleted):
see ``publish/retention.py`` and ``publish/deletions.py``."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select

from africasignal.jobs.execution import fenced_effect
from africasignal.jobs.handlers import JobContext, register
from africasignal.models import EvidenceDocument
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
    deletions.mirror_pending(ctx.session, store, datetime.now(UTC))


@register("purge_expired_evidence")
def purge_expired_evidence(ctx: JobContext) -> None:
    key = str(ctx.job.payload.get("storage_key", ""))
    if not key.startswith("evidence/"):
        raise ValueError("only evidence objects may be purged")
    # Content-addressed objects can be shared by another source with a longer permission.
    live = ctx.session.scalar(
        select(EvidenceDocument.id)
        .where(EvidenceDocument.storage_key == key, EvidenceDocument.status != "expired")
        .limit(1)
    )
    if live is not None:
        return
    store = store_for_session(ctx.session)
    if store is None:
        raise RuntimeError("Evidence purge requires object storage")
    with fenced_effect():
        store.delete(key)
