"""Cross-site request forgery guard for the public site's forms (spec B11.3).

The same origin check as the operator console guard (``csrf.py``): an unsafe request must come from
a page on an origin the site trusts, judged by ``Origin`` and then ``Referer``. The trusted origins
are the request's own, the site's public address (read from the console settings, so the site
works behind a TLS-terminating proxy once the address is set) and ``ADMIN_TRUSTED_ORIGINS``.

Not guarded, on purpose:

* ``/admin``: it has its own guard.
* ``/unsubscribe``: a mail client's one-click unsubscribe (RFC 8058) is a POST that carries no
  ``Origin``. The link holds a signed token, and unsubscribing is harmless to repeat.
* ``/places/locate``: changes nothing and needs no cookie.
"""

from __future__ import annotations

import logging
import os

from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from africasignal import settings_store
from africasignal.db import session_scope
from africasignal.web.csrf import request_origin_allowed

log = logging.getLogger("africasignal.web.csrf")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
EXEMPT_PREFIXES = ("/admin", "/places/locate", "/static/")
EXEMPT_PATHS = ("/unsubscribe",)


def _guarded(path: str) -> bool:
    return path not in EXEMPT_PATHS and not any(
        path == p.rstrip("/") or path.startswith(p) for p in EXEMPT_PREFIXES
    )


def _trusted_raw() -> str:
    """``ADMIN_TRUSTED_ORIGINS`` plus the site's public address. A database that cannot be read
    leaves just the first, so a fault never widens what is trusted."""
    trusted = os.environ.get("ADMIN_TRUSTED_ORIGINS", "")
    try:
        with session_scope() as session:
            address = settings_store.get(session, "public_base_url")
    except Exception:
        log.exception("could not read the public address for the origin check")
        address = None
    return f"{trusted},{address}" if address else trusted


class PublicOriginGuard:
    """ASGI middleware: refuse unsafe requests to the public site from untrusted origins."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method", "GET").upper() not in _SAFE_METHODS
            and _guarded(scope.get("path", ""))
        ):
            trusted_raw = await run_in_threadpool(_trusted_raw)
            if not request_origin_allowed(scope, list(scope.get("headers") or []), trusted_raw):
                log.warning("refused cross-origin %s %s", scope.get("method"), scope.get("path"))
                response = PlainTextResponse("Cross-origin request refused.", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
