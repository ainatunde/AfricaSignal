"""The address of the person making a request, for rate limits (AS-042, finding S-03).

Every limit (sign-in, feedback, API, location, console sign-in) is keyed on this address, and the
address is only ever held in memory as a salted hash or as a keyed hash (see ``ratelimit.py`` and
``operators.client_key``); it is never stored or logged.

Behind a reverse proxy the connecting address is the proxy's, so all readers would share one
limit. The operator says how many proxies sit in front of the app (Settings > Website > Proxies in
front of the site, ``trusted_proxy_hops``). With ``n`` proxies, each appending the address it saw
to ``X-Forwarded-For``, the reader is the ``n``-th entry from the right: entries further left were
written by the reader and can be anything, so they are never used. If the header is missing, too
short or not an address, the connecting address is used. 0 (the default) never reads the header.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from collections.abc import Callable

from starlette.requests import Request

from africasignal import settings_store
from africasignal.db import session_scope

log = logging.getLogger("africasignal.web.client_address")

SETTING = "trusted_proxy_hops"
CACHE_SECONDS = 30.0


def _read_hops() -> int:
    try:
        with session_scope() as session:
            return max(0, settings_store.get_int(session, SETTING) or 0)
    except Exception:
        log.exception("could not read %s; using the connecting address", SETTING)
        return 0


# Replaceable in tests. The value is cached briefly so a request does not cost a query.
hops_source: Callable[[], int] = _read_hops
_cached: tuple[float, int] | None = None


def trusted_proxy_hops() -> int:
    global _cached
    now = time.monotonic()
    if _cached is None or now - _cached[0] > CACHE_SECONDS:
        _cached = (now, hops_source())
    return _cached[1]


def reset_cache() -> None:
    global _cached
    _cached = None


def forwarded_client(header_values: list[str], hops: int) -> str | None:
    """The reader's address in ``X-Forwarded-For`` given ``hops`` trusted proxies, or None."""
    entries = [e.strip() for value in header_values for e in value.split(",")]
    if hops < 1 or len(entries) < hops:
        return None
    candidate = entries[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def trusted_proxy_peer(peer: str) -> bool:
    """Trust forwarding headers only from an explicitly configured proxy network."""
    try:
        address = ipaddress.ip_address(peer)
        networks = [
            ipaddress.ip_network(value.strip())
            for value in os.environ.get("TRUSTED_PROXY_NETWORKS", "").split(",")
            if value.strip()
        ]
        return any(address in network for network in networks)
    except ValueError:
        return False


def client_address(request: Request) -> str:
    """The reader's address, for a limiter key. Never stored or logged."""
    peer = request.client.host if request.client else "unknown"
    hops = trusted_proxy_hops()
    if hops and trusted_proxy_peer(peer):
        forwarded = forwarded_client(request.headers.getlist("x-forwarded-for"), hops)
        if forwarded is not None:
            return forwarded
    return peer
