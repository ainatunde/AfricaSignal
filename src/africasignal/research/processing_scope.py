"""Task-local narrowing of existing adapter writes; normal ingestion is unaffected."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("research_processing_scope", default=None)


@contextmanager
def processing_scope(scope: dict[str, Any]) -> Iterator[None]:
    token = CURRENT.set(scope)
    try:
        yield
    finally:
        CURRENT.reset(token)
