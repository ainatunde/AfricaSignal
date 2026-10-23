"""Optional task-local host and request budget. Ordinary collection keeps its existing policy."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit


class ScopeRefused(ValueError):
    pass


@dataclass(frozen=True)
class RequestScope:
    hosts: frozenset[str]
    reserve: Callable[[], None]

    def admit(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ScopeRefused("research_source_host_not_approved")
        self.reserve()


CURRENT: ContextVar[RequestScope | None] = ContextVar("research_request_scope", default=None)


@contextmanager
def request_scope(scope: RequestScope) -> Iterator[None]:
    token = CURRENT.set(scope)
    try:
        yield
    finally:
        CURRENT.reset(token)
