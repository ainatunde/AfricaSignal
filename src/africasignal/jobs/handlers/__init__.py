"""Handler registry: one handler per job kind."""

from __future__ import annotations

import importlib
import pkgutil
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
# What to tidy up when a job of this kind has used all its attempts and is marked ``dead``.
DEAD_HOOKS: dict[str, Callable[[ClaimedJob], None]] = {}


def register(kind: str) -> Callable[[Handler], Handler]:
    """Decorator: ``@register("fetch_source")``. A kind can have only one handler."""

    def decorator(fn: Handler) -> Handler:
        if kind in HANDLERS:
            raise ValueError(f"handler already registered for {kind!r}")
        HANDLERS[kind] = fn
        return fn

    return decorator


def on_dead(kind: str) -> Callable[[Callable[[ClaimedJob], None]], Callable[[ClaimedJob], None]]:
    """Decorator: ``@on_dead("import_nbs_file")``. The function runs once, after the worker marks a
    job of that kind dead, to release what the job was holding (an uploaded file, say)."""

    def decorator(fn: Callable[[ClaimedJob], None]) -> Callable[[ClaimedJob], None]:
        if kind in DEAD_HOOKS:
            raise ValueError(f"dead-job hook already registered for {kind!r}")
        DEAD_HOOKS[kind] = fn
        return fn

    return decorator


def get_handler(kind: str) -> Handler | None:
    return HANDLERS.get(kind)


def load_all() -> None:
    """Import every handler module, and every source adapter module, so its ``@register`` runs.
    Called by the worker and the scheduler at startup. Modules are found, not listed, so a new
    handler file cannot be forgotten here and left as a job kind nobody runs."""
    for name in (__name__, "africasignal.sources"):
        package = importlib.import_module(name)
        for module in pkgutil.iter_modules(package.__path__):
            if not module.name.startswith("_"):
                importlib.import_module(f"{name}.{module.name}")
