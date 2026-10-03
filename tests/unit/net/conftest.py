"""Offline harness for the network layer: fake DNS and a fake web behind the pinned transport."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from africasignal.net import fetch, netutil, politeness

PUBLIC_IP = "93.184.216.34"

# Hostnames with a scripted DNS answer. Anything else resolves to PUBLIC_IP.
DNS_TABLE: dict[str, list[str]] = {
    "internal.example": ["10.0.0.5"],
    "metadata.example": ["169.254.169.254"],
    "loopback.example": ["127.0.0.1"],
    "mixed.example": [PUBLIC_IP, "192.168.1.10"],  # one public and one private answer
    "v6-local.example": ["::1"],
}

_real_getaddrinfo = socket.getaddrinfo


def _addrinfo(ip: str, port: int) -> tuple[Any, ...]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Resolve names from ``DNS_TABLE`` without touching the network."""
    table = dict(DNS_TABLE)

    def getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host == "localhost":
                return [_addrinfo("127.0.0.1", port)]
            return [_addrinfo(ip, port) for ip in table.get(host.lower(), [PUBLIC_IP])]
        return list(_real_getaddrinfo(host, port, *args, **kwargs))

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(
        netutil,
        "_dns_addresses",
        lambda host, port: [str(x[4][0]) for x in getaddrinfo(host, port)],
    )
    return table


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh robots cache and a generous shared rate limiter for every test."""
    netutil.reset_cache()
    monkeypatch.setattr(
        politeness, "rate_limiter", politeness.TokenBucketRateLimiter(rate=1000, capacity=1000)
    )
    yield
    netutil.reset_cache()


class FakeWeb:
    """Serves canned responses through ``httpx.MockTransport``. Unknown URLs are 404."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def add(
        self,
        url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
        stream: httpx.SyncByteStream | None = None,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if handler is not None:
                return handler(request)
            if stream is not None:
                return httpx.Response(status, headers=headers, stream=stream)
            return httpx.Response(status, headers=headers, content=content)

        self.routes[url] = respond

    def redirect(self, url: str, to: str, status: int = 302) -> None:
        self.add(url, status=status, headers={"Location": to})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404, content=b"not found")
        return route(request)

    def fetched(self) -> list[str]:
        """URLs requested, robots.txt lookups excluded."""
        return [str(r.url) for r in self.requests if r.url.path != "/robots.txt"]

    def header(self, url: str, name: str) -> str | None:
        for r in self.requests:
            if str(r.url) == url:
                return r.headers.get(name)
        return None


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    fake = FakeWeb()

    def transport(url: str) -> httpx.MockTransport:
        if not netutil.is_safe_public_url(url):  # same contract as the real factory
            raise ValueError("URL does not resolve entirely to public addresses")
        return httpx.MockTransport(fake.handle)

    monkeypatch.setattr(fetch, "pinned_http_transport", transport)
    monkeypatch.setattr(netutil, "pinned_http_transport", transport)
    return fake


class ChunkedStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
