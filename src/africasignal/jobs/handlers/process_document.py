"""``process_document``: capture one discovered document and turn it into data (spec B6.2)."""

from __future__ import annotations

import logging
from datetime import datetime

from africasignal.evidence.capture import capture
from africasignal.jobs.handlers import JobContext, register
from africasignal.models import Source
from africasignal.publish.situations import request_assessments
from africasignal.sources.base import AdapterContext, get_adapter
from africasignal.sources.health import record_failure
from africasignal.storage import get_store

log = logging.getLogger("africasignal.process_document")


@register("process_document")
def process_document(ctx: JobContext) -> None:
    session = ctx.session
    payload = ctx.job.payload
    source = session.get(Source, payload["source_id"])
    if source is None or not source.active:
        log.info("skipping process_document: source missing or inactive")
        return
    adapter = get_adapter(source.adapter)
    if adapter is None:
        log.info("no adapter implemented yet for %s (%s)", source.slug, source.adapter)
        return

    published_at = (
        datetime.fromisoformat(payload["published_at"]) if "published_at" in payload else None
    )
    store = get_store()
    try:
        # capture refuses when the source has no approved permission to collect
        document = capture(
            session,
            store,
            source,
            payload["url"],
            published_at=published_at,
            title=payload.get("title"),
        )
        result = adapter.process(document, AdapterContext(session=session, store=store))
    except Exception as exc:
        record_failure(session, source.id, f"{type(exc).__name__}: {exc}")
        raise
    queued = request_assessments(session, result.touched, document.id, result.superseded)
    log.info(
        "processed %s: %d measurements, %d claims, %d assessments queued",
        payload["url"],
        result.measurements,
        result.claims,
        len(queued),
        extra={"job_id": ctx.job.id},
    )
    for note in result.notes:
        log.info("%s: %s", payload["url"], note, extra={"job_id": ctx.job.id})
