"""Browser security headers for every response (AS-042).

The public pages use no inline script or style and load nothing from another site, so a strict
Content-Security-Policy costs nothing and means that a stray ``javascript:`` link or an injected tag
cannot run. The console sets its own, stricter headers (``routes/admin.py``); this middleware only
fills in a header a response does not already carry, so the console's values are kept and the
plain-text errors produced by the origin guards get the defaults.

``Strict-Transport-Security`` is sent outside development. Browsers ignore it over plain http, so
it does no harm behind a TLS-terminating proxy that talks http to the app.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from africasignal.config import get_settings

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

DEFAULT_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), payment=()",
}
HSTS = "max-age=31536000"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in DEFAULT_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
                if (
                    "Strict-Transport-Security" not in headers
                    and get_settings().env != "development"
                ):
                    headers["Strict-Transport-Security"] = HSTS
            await send(message)

        await self.app(scope, receive, send_with_headers)
