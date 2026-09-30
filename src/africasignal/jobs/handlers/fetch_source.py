"""``fetch_source``: check a source for new documents (spec B4, B6.2)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from africasignal.evidence.urls import canonicalise
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext, register
from africasignal.models import Source
from africasignal.sources.base import AdapterContext, get_adapter
from africasignal.sources.health import record_failure
from africasignal.sources.permissions import current_permission
from africasignal.storage import get_store

log = logging.getLogger("africasignal.fetch_source")


@register("fetch_source")
def fetch_source(ctx: JobContext) -> None:
    session = ctx.session
    source = session.get(Source, ctx.job.payload["source_id"])
    if source is None or not source.active:
        log.info("skipping fetch_source: source missing or inactive", extra={"job_id": ctx.job.id})
        return

    permission = current_permission(session, source.id)
    if permission is None or not permission.may_collect:
        log.warning(
            "refusing to fetch source %s: it has no approved permission to collect",
            source.slug,
            extra={"job_id": ctx.job.id},
        )
        return

    adapter = get_adapter(source.adapter)
    if adapter is None:
        log.info("no adapter implemented yet for %s (%s)", source.slug, source.adapter)
        return

    try:
        items = adapter.discover(source, AdapterContext(session=session, store=get_store()))
    except Exception as exc:
        record_failure(session, source.id, f"{type(exc).__name__}: {exc}")
        raise

    for item in items:
        # One process_document job per canonical URL, however often the listing repeats it.
        payload: dict[str, object] = {"source_id": source.id, "url": item.url}
        if item.title:
            payload["title"] = item.title
        if item.published_at:
            payload["published_at"] = item.published_at.isoformat()
        if item.byline:
            payload["byline"] = item.byline
        queue.enqueue(
            session,
            "process_document",
            payload,
            dedupe_key=f"process_document:{source.id}:{canonicalise(item.url)}",
        )
    source.last_success_at = datetime.now(UTC)
    source.consecutive_failures = 0
    source.last_error = None
    source.health = "healthy"
    log.info("source %s: %d items listed", source.slug, len(items))
