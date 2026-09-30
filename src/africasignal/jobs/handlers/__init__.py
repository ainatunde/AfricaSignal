"""Handler registry: one handler per job kind."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from africasignal.jobs.queue import ClaimedJob


@dataclass(frozen=True)
class JobContext:
    session: Session
    job: ClaimedJob
    worker_id: str


Handler = Callable[[JobContext], None]

HANDLERS: dict[str, Handler] = {}


def register(kind: str) -> Callable[[Handler], Handler]:
    """Decorator: ``@register("fetch_source")``. A kind can have only one handler."""

    def decorator(fn: Handler) -> Handler:
        if kind in HANDLERS:
            raise ValueError(f"handler already registered for {kind!r}")
        HANDLERS[kind] = fn
        return fn

    return decorator


def get_handler(kind: str) -> Handler | None:
    return HANDLERS.get(kind)


def load_all() -> None:
    """Import every handler module so its ``@register`` runs. Called by the worker and the
    scheduler at startup."""
    from africasignal.jobs.handlers import (  # noqa: F401
        apply_retention,
        assess_situation,
        dispatch_outbox,
        expire_assessments,
        extract_claims,
        fetch_source,
        gdelt_fetch_article,
        gdelt_poll,
        import_nbs_file,
        invalidate,
        notify_followers,
        process_document,
        prune_events,
        release_held_versions,
        resolve_places,
        weekly_digest,
    )
    from africasignal.sources import (  # noqa: F401  (register the source adapters)
        gdelt,
        nbs,
        nerc,
        price_announcements,
        rss,
    )
