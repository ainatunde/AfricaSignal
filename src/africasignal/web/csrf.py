"""Cross-site request forgery guard for the operator console (``/admin``).

Adapted from the TV Insights console guard. The console cookie is ``SameSite=Strict``, which
already stops cross-site POSTs; this check also refuses requests from same-site origins (a sibling
subdomain, another port on the same host) and a login form posted from elsewhere. Every
state-changing request under ``/admin`` must come from a page on an origin the console trusts:

* ``Origin`` present: it must be trusted. It wins over ``Referer``.
* ``Origin`` absent: the origin of ``Referer`` must be trusted.
* Neither: refused. Browsers send ``Origin`` on every form POST.

Trusted origins are the request's own (scheme, ``Host``, port as the server received them) plus the
exact origins in the ``ADMIN_TRUSTED_ORIGINS`` environment variable, for deployments behind a
TLS-terminating proxy where the app sees ``http://internal`` and the browser sends
``https://public``. It is read on every request and is never an in-app setting, so the console
cannot widen its own boundary. ``X-Forwarded-Host`` is never consulted.

Origins compare exactly after normalisation (scheme and host case-insensitive, default ports
implied). Anything that is not a plain ``scheme://host[:port]`` (userinfo, a path, a wildcard,
``null``, several values) matches nothing. The check runs as ASGI middleware before routing and
body parsing, so a refused request has no side effect.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("africasignal.web.csrf")

Origin = tuple[str, str, int]  # (scheme, host, port)

GUARDED_PREFIX = "/admin"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_DEFAULT_PORT = {"http": 80, "https": 443}
_SERIALISED_ORIGIN = re.compile(r"(?i)(https?)://([^/?#@\s,\\]+)")
_HOST = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::([0-9]{1,5}))?")
_IPV6_HOST = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::([0-9]{1,5}))?")


def _host_port(hostport: str, scheme: str) -> tuple[str, int] | None:
    match = _IPV6_HOST.fullmatch(hostport) or _HOST.fullmatch(hostport)
    if match is None:
        return None
    host, port_text = match.group(1).lower(), match.group(2)
    port = int(port_text) if port_text is not None else _DEFAULT_PORT[scheme]
    if not 0 < port < 65536:
        return None
    return host, port


def parse_origin(value: str) -> Origin | None:
    """A serialised origin (``scheme://host[:port]``, nothing else), normalised; else None."""
    match = _SERIALISED_ORIGIN.fullmatch(value)
    if match is None:
        return None
    scheme = match.group(1).lower()
    parsed = _host_port(match.group(2), scheme)
    if parsed is None:
        return None
    return scheme, parsed[0], parsed[1]


def referer_origin(value: str) -> Origin | None:
    """The origin of an absolute http(s) ``Referer`` URL without userinfo; else None."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in _DEFAULT_PORT or not parts.netloc or "@" in parts.netloc:
        return None
    return parse_origin(f"{parts.scheme}://{parts.netloc}")


def trusted_origins(raw: str) -> frozenset[Origin]:
    """Parse ``ADMIN_TRUSTED_ORIGINS``. Entries that are not exact origins are ignored."""
    parsed: set[Origin] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        origin = parse_origin(entry[:-1] if entry.endswith("/") else entry)  # one trailing slash
        if origin is None:
            log.warning(
                "ADMIN_TRUSTED_ORIGINS entry %r is not an exact origin and is ignored", entry
            )
            continue
        parsed.add(origin)
    return frozenset(parsed)


def _own_origin(scope: Scope, host_header: str | None) -> Origin | None:
    scheme = str(scope.get("scheme", "http")).lower()
    if scheme not in _DEFAULT_PORT:
        return None
    if host_header is not None:
        parsed = _host_port(host_header, scheme)
    else:
        server = scope.get("server")
        parsed = _host_port(f"{server[0]}:{server[1]}", scheme) if server else None
    if parsed is None:
        return None
    return scheme, parsed[0], parsed[1]


def request_origin_allowed(
    scope: Scope, headers: list[tuple[bytes, bytes]], trusted_raw: str
) -> bool:
    """Whether an unsafe request carries an Origin (or Referer) the console trusts."""

    def values(name: bytes) -> list[str]:
        return [v.decode("latin-1") for k, v in headers if k.lower() == name]

    hosts = values(b"host")
    if len(hosts) > 1:
        return False
    trusted = set(trusted_origins(trusted_raw))
    own = _own_origin(scope, hosts[0] if hosts else None)
    if own is not None:
        scheme, host, _port = own
        # Behind a proxy that keeps the public Host over plain http, http://<public host> would
        # look like the request's own origin. When the https form is listed, don't trust http.
        if not (scheme == "http" and any(t[0] == "https" and t[1] == host for t in trusted)):
            trusted.add(own)

    origins = values(b"origin")
    if len(origins) > 1:
        return False
    if origins:
        candidate = parse_origin(origins[0])
    else:
        referers = values(b"referer")
        if len(referers) != 1:
            return False
        candidate = referer_origin(referers[0])
    return candidate is not None and candidate in trusted


def _guarded(path: str) -> bool:
    return path == GUARDED_PREFIX or path.startswith(GUARDED_PREFIX + "/")


class AdminOriginGuard:
    """ASGI middleware: refuse unsafe ``/admin`` requests from untrusted origins with 403."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method", "GET").upper() not in _SAFE_METHODS
            and _guarded(scope.get("path", ""))
        ):
            trusted_raw = os.environ.get("ADMIN_TRUSTED_ORIGINS", "")
            if not request_origin_allowed(scope, list(scope.get("headers") or []), trusted_raw):
                log.warning("refused cross-origin %s %s", scope.get("method"), scope.get("path"))
                response = PlainTextResponse("Cross-origin request refused.", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
