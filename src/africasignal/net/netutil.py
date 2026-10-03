"""SSRF guard, DNS-pinned transport and robots.txt compliance.

Copied from TV Insights (``app/curation/netutil.py``) and adapted: new User-Agent, robots.txt
answers expire after an hour, and the robots.txt body is size-capped.

Feeds fetched over http(s) are checked against the host's robots.txt before we
request them. Network failures, server errors and unhandled redirects fail closed.
An absent robots.txt (4xx) permits acquisition; explicit Disallow is honoured.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpcore
import httpx

from africasignal.net.resolver import dns_addresses as _dns_addresses
from africasignal.net.resolver import dns_budget

log = logging.getLogger("africasignal.net.netutil")

# The bot-info page is served at /about/bot. The domain is not chosen yet (plan decision D7), so
# it is configurable.
BOT_INFO_URL = os.environ.get("BOT_INFO_URL", "https://africasignal.example/about/bot")
USER_AGENT = f"AfricaSignalBot/1.0 (+{BOT_INFO_URL})"

ROBOTS_TTL_SECONDS = 3600.0
ROBOTS_MAX_BYTES = 500_000

# base URL -> (fetched at monotonic time, parser or None meaning "no robots.txt: allow")
_robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}


def _robots_for(host_url: str, timeout: float | None = None) -> RobotFileParser | None:
    parsed = urlparse(host_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    cached = _robots_cache.get(base)
    if cached is not None and time.monotonic() - cached[0] < ROBOTS_TTL_SECONDS:
        return cached[1]
    parser = RobotFileParser()
    parser.set_url(f"{base}/robots.txt")
    rp: RobotFileParser | None = parser
    request_budget = min(10.0, timeout) if timeout is not None else 10.0
    if request_budget <= 0:
        parser.parse(["User-agent: *", "Disallow: /"])
        return parser
    deadline = time.monotonic() + request_budget
    failed = False
    try:
        with dns_budget(request_budget):
            transport = pinned_http_transport(host_url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("robots preflight deadline exceeded")
        with (
            httpx.Client(
                timeout=remaining,
                trust_env=False,
                transport=transport,
            ) as client,
            client.stream("GET", f"{base}/robots.txt", headers={"User-Agent": USER_AGENT}) as resp,
        ):
            if 400 <= resp.status_code < 500:
                rp = None  # robots unavailable (RFC 9309)
            elif not resp.is_success:
                parser.parse(["User-agent: *", "Disallow: /"])
            else:
                body = b""
                for chunk in resp.iter_bytes():
                    if time.monotonic() > deadline:
                        raise TimeoutError("robots preflight deadline exceeded")
                    body += chunk
                    if len(body) >= ROBOTS_MAX_BYTES:
                        break
                parser.parse(body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace").splitlines())
    except Exception:  # unreachable robots must disallow (RFC 9309)
        parser.parse(["User-agent: *", "Disallow: /"])
        rp = parser
        failed = True
    # A request bounded by a caller's shorter fetch deadline must not poison the shared cache if
    # it times out. The next fetch can retry with a fresh deadline.
    if not (failed and timeout is not None):
        _robots_cache[base] = (time.monotonic(), rp)
    return rp


def allowed_by_robots(
    url: str, user_agent: str = USER_AGENT, *, timeout: float | None = None
) -> bool:
    """True if the URL may be fetched per the host's robots.txt (or non-http)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True  # file:// and local paths aren't governed by robots
    rp = _robots_for(url, timeout=timeout)
    if rp is None:
        return True
    return rp.can_fetch(user_agent, url)


def robots_pacing(
    url: str, user_agent: str = USER_AGENT, *, timeout: float | None = None
) -> tuple[float | None, tuple[int, int] | None]:
    """Return this agent's optional crawl-delay and request-rate from cached robots rules."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, None
    rp = _robots_for(url, timeout=timeout)
    if rp is None:
        return None, None
    delay = rp.crawl_delay(user_agent)
    try:
        delay_seconds = float(delay) if delay is not None else None
    except (TypeError, ValueError):
        delay_seconds = None
    request_rate = rp.request_rate(user_agent)
    rate = (
        (request_rate.requests, request_rate.seconds)
        if request_rate is not None and request_rate.requests > 0 and request_rate.seconds > 0
        else None
    )
    return (delay_seconds if delay_seconds is not None and delay_seconds > 0 else None), rate


def reset_cache() -> None:
    _robots_cache.clear()


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable -> block
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # ::ffff:127.0.0.1 is loopback
    return (
        not addr.is_global  # also covers carrier-grade NAT (100.64.0.0/10) and documentation ranges
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


@dataclass(frozen=True)
class _ApprovedRemote:
    """A public DNS answer that may be used for one hostname-preserving dial."""

    host: str
    port: int
    addresses: tuple[str, ...]


def _resolve_public_host(host: str, port: int) -> _ApprovedRemote | None:
    """Resolve one TCP origin and fail closed unless every IP is public."""
    try:
        # Literal addresses need no resolver but pass through the identical public-IP guard.
        ips = {str(ipaddress.ip_address(host))}
    except ValueError:
        ips = set(_dns_addresses(host, port))
    if not ips or any(_ip_is_blocked(ip) for ip in ips):
        return None
    return _ApprovedRemote(
        host=host.lower(),
        port=port,
        addresses=tuple(sorted(ips)),
    )


def _resolve_public_remote(url: str) -> _ApprovedRemote | None:
    """Resolve an HTTP(S) target once and fail closed unless every IP is public."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return _resolve_public_host(parsed.hostname, port)


def is_safe_public_url(url: str) -> bool:
    """True only for HTTP(S) targets that resolve entirely to public addresses."""
    return _resolve_public_remote(url) is not None


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    """Prevent HTTPX/httpcore from resolving an approved hostname a second time."""

    def __init__(self, approved: _ApprovedRemote) -> None:
        self._approved_by_origin = {(approved.host, approved.port): approved}
        self._delegate = httpcore.SyncBackend()

    def _approved_for(self, host: str, port: int) -> _ApprovedRemote | None:
        key = (host.lower(), port)
        approved = self._approved_by_origin.get(key)
        if approved is None:
            # Redirects retain their URL hostname. Resolve the new origin at
            # the actual dial boundary and pin that answer before TCP opens.
            approved = _resolve_public_host(*key)
            if approved is not None:
                self._approved_by_origin[key] = approved
        return approved

    def pinned_ip_for(self, host: str, port: int) -> str | None:
        approved = self._approved_for(host, port)
        return approved.addresses[0] if approved is not None else None

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        started = time.monotonic()
        with dns_budget(timeout if timeout is not None else 5.0):
            approved = self._approved_for(host, port)
        if approved is None:
            raise httpcore.ConnectError("Pinned transport rejected an unapproved destination")

        last_error: httpcore.ConnectError | None = None
        for address in approved.addresses:
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("Connection deadline exceeded")
            try:
                return self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError("No approved address could be reached")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Pinned transport does not permit Unix-socket connections")

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    """Keep HTTPS hostname/SNI validation while pinning its TCP destination."""

    def __init__(self, approved: _ApprovedRemote) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pinned_backend = _PinnedNetworkBackend(approved)
        # HTTPX does not publish a resolver hook. This httpcore backend is where
        # TCP dialing happens; TLS still receives the original origin hostname.
        self._pool._network_backend = self._pinned_backend

    def pinned_ip_for(self, host: str, port: int) -> str | None:
        return self._pinned_backend.pinned_ip_for(host, port)


def pinned_http_transport(url: str) -> httpx.HTTPTransport:
    """Build a hostname-preserving transport pinned to this URL's public DNS set.

    Callers must create a new transport for each redirect hop. A later DNS
    answer cannot redirect the TCP socket to a private or loopback target.
    """
    approved = _resolve_public_remote(url)
    if approved is None:
        raise ValueError("URL does not resolve entirely to public addresses")
    return _PinnedHTTPTransport(approved)


__all__ = [
    "BOT_INFO_URL",
    "USER_AGENT",
    "allowed_by_robots",
    "is_safe_public_url",
    "pinned_http_transport",
    "reset_cache",
]
