"""SSRF guard, DNS pinning and robots.txt caching."""

import socket
from typing import Any

import httpcore
import httpx
import pytest

from africasignal.net import netutil

from .conftest import PUBLIC_IP, FakeWeb


@pytest.mark.parametrize(
    "url",
    [
        "https://news.example.ng/a",
        "http://example.org:8080/x",
    ],
)
def test_public_urls_are_safe(url: str) -> None:
    assert netutil.is_safe_public_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://100.64.0.1/",  # carrier-grade NAT is not public either
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://[::ffff:10.0.0.1]/",
        "http://224.0.0.1/",  # multicast
        "http://0.0.0.0/",
        "http://internal.example/",
        "http://mixed.example/",
        "ftp://news.example.ng/",
        "https:///nohost",
        "http://news.example.ng:notaport/",
    ],
)
def test_unsafe_urls_are_refused(url: str) -> None:
    assert netutil.is_safe_public_url(url) is False


def test_pinned_transport_is_refused_for_private_targets() -> None:
    with pytest.raises(ValueError):
        netutil.pinned_http_transport("http://internal.example/")


def test_pinned_transport_dials_the_approved_address_even_if_dns_rebinds(
    monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, list[str]]
) -> None:
    """A host that resolves publicly when approved and to loopback afterwards is still dialled at
    the approved public address."""
    transport = netutil.pinned_http_transport("https://rebind.example/feed")
    fake_dns["rebind.example"] = ["127.0.0.1"]  # the attacker flips the record after approval

    dialled: list[str] = []
    backend = transport._pinned_backend  # type: ignore[attr-defined]

    def record(address: str, port: int, **kwargs: Any) -> httpcore.NetworkStream:
        dialled.append(address)
        raise httpcore.ConnectError("stop here")

    monkeypatch.setattr(backend._delegate, "connect_tcp", record)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("rebind.example", 443)
    assert dialled == [PUBLIC_IP]
    assert transport.pinned_ip_for("rebind.example", 443) == PUBLIC_IP  # type: ignore[attr-defined]


def test_pinned_transport_rejects_origins_it_did_not_approve_if_they_are_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = netutil.pinned_http_transport("https://news.example.ng/")
    backend = transport._pinned_backend  # type: ignore[attr-defined]
    monkeypatch.setattr(backend._delegate, "connect_tcp", lambda *a, **k: pytest.fail("dialled"))
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("internal.example", 443)  # for example a redirect target


def test_pinned_transport_falls_back_across_approved_addresses(
    monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, list[str]]
) -> None:
    fake_dns["multi.example"] = ["93.184.216.40", "93.184.216.41"]
    transport = netutil.pinned_http_transport("https://multi.example/")
    backend = transport._pinned_backend  # type: ignore[attr-defined]
    tried: list[str] = []

    def flaky(address: str, port: int, **kwargs: Any) -> str:
        tried.append(address)
        if address.endswith(".40"):
            raise httpcore.ConnectError("down")
        return "stream"

    monkeypatch.setattr(backend._delegate, "connect_tcp", flaky)
    assert backend.connect_tcp("multi.example", 443) == "stream"
    assert tried == ["93.184.216.40", "93.184.216.41"]


def test_pinned_transport_refuses_unix_sockets() -> None:
    backend = netutil.pinned_http_transport("https://news.example.ng/")._pinned_backend  # type: ignore[attr-defined]
    with pytest.raises(httpcore.ConnectError):
        backend.connect_unix_socket("/var/run/docker.sock")


# --- robots.txt --------------------------------------------------------------------------------


def test_robots_disallow_and_allow(web: FakeWeb) -> None:
    web.add(
        "https://site.example.ng/robots.txt",
        content=b"User-agent: *\nDisallow: /private/\n",
    )
    assert netutil.allowed_by_robots("https://site.example.ng/public/a") is True
    assert netutil.allowed_by_robots("https://site.example.ng/private/a") is False


def test_robots_answer_is_cached_then_expires(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    web.add("https://site.example.ng/robots.txt", content=b"User-agent: *\nDisallow:\n")
    now = [1000.0]
    monkeypatch.setattr(netutil.time, "monotonic", lambda: now[0])

    def robots_requests() -> int:
        return sum(1 for r in web.requests if r.url.path == "/robots.txt")

    netutil.allowed_by_robots("https://site.example.ng/a")
    netutil.allowed_by_robots("https://site.example.ng/b")
    assert robots_requests() == 1
    now[0] += netutil.ROBOTS_TTL_SECONDS + 1
    web.add("https://site.example.ng/robots.txt", content=b"User-agent: *\nDisallow: /\n")
    assert netutil.allowed_by_robots("https://site.example.ng/a") is False
    assert robots_requests() == 2


def test_robots_body_is_size_capped(web: FakeWeb) -> None:
    filler = b"# " + b"x" * (netutil.ROBOTS_MAX_BYTES + 10_000) + b"\n"
    # A Disallow that sits beyond the cap is not read.
    web.add("https://site.example.ng/robots.txt", content=filler + b"User-agent: *\nDisallow: /\n")
    assert netutil.allowed_by_robots("https://site.example.ng/a") is True


def test_robots_server_error_means_allow(web: FakeWeb) -> None:
    web.add("https://site.example.ng/robots.txt", status=500, content=b"oops")
    assert netutil.allowed_by_robots("https://site.example.ng/a") is True


def test_non_http_urls_are_not_governed_by_robots() -> None:
    assert netutil.allowed_by_robots("file:///tmp/x") is True


def test_getaddrinfo_is_never_called_for_the_real_network(fake_dns: dict[str, list[str]]) -> None:
    """Guard on the harness itself: names resolve from the table, not from real DNS."""
    infos = socket.getaddrinfo("anything.example", 443)
    assert infos[0][4][0] == PUBLIC_IP
    assert isinstance(web_dummy := httpx.Client, type) and web_dummy is httpx.Client
