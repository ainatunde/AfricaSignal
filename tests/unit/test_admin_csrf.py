"""The origin guard for /admin (ported behaviour from the TV Insights console guard)."""

from __future__ import annotations

import pytest
from starlette.types import Scope

from africasignal.web.csrf import (
    parse_origin,
    referer_origin,
    request_origin_allowed,
    trusted_origins,
)


def _scope(scheme: str = "http") -> Scope:
    return {"type": "http", "scheme": scheme, "server": ("testserver", 80)}


def _allowed(headers: dict[str, str | list[str]], trusted: str = "", scheme: str = "http") -> bool:
    raw: list[tuple[bytes, bytes]] = []
    for name, value in headers.items():
        for v in [value] if isinstance(value, str) else value:
            raw.append((name.lower().encode(), v.encode()))
    return request_origin_allowed(_scope(scheme), raw, trusted)


def test_same_origin_post_is_allowed() -> None:
    assert _allowed({"host": "console.example.org", "origin": "http://console.example.org"})


def test_default_port_is_implied_and_host_is_case_insensitive() -> None:
    assert _allowed({"host": "Console.Example.org:80", "origin": "http://console.example.ORG"})


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example.org",
        "https://console.example.org",  # other scheme
        "http://console.example.org:8080",  # other port
        "http://sub.console.example.org",
        "null",
        "http://console.example.org/path",
        "http://user@console.example.org",
        "*",
        "",
    ],
)
def test_other_origins_are_refused(origin: str) -> None:
    assert not _allowed({"host": "console.example.org", "origin": origin})


def test_missing_origin_and_referer_is_refused() -> None:
    assert not _allowed({"host": "console.example.org"})


def test_referer_used_only_without_origin() -> None:
    host = {"host": "console.example.org"}
    assert _allowed({**host, "referer": "http://console.example.org/admin/sources"})
    assert not _allowed({**host, "referer": "http://evil.example.org/admin"})
    # a cross-origin Origin wins over a same-origin Referer
    assert not _allowed(
        {**host, "origin": "http://evil.example.org", "referer": "http://console.example.org/x"}
    )


def test_duplicate_headers_are_refused() -> None:
    assert not _allowed({"host": "a.example.org", "origin": ["http://a.example.org"] * 2})
    assert not _allowed(
        {"host": ["a.example.org", "b.example.org"], "origin": "http://a.example.org"}
    )


def test_trusted_origins_setting_allows_the_public_https_origin() -> None:
    trusted = "https://console.example.org/, junk, http://x.example.org/path"
    assert _allowed({"host": "internal:8000", "origin": "https://console.example.org"}, trusted)
    assert not _allowed({"host": "internal:8000", "origin": "https://other.example.org"}, trusted)
    assert trusted_origins(trusted) == {("https", "console.example.org", 443)}


def test_plain_http_form_of_a_listed_https_host_is_not_trusted() -> None:
    trusted = "https://console.example.org"
    assert not _allowed(
        {"host": "console.example.org", "origin": "http://console.example.org"}, trusted
    )


def test_parsers() -> None:
    assert parse_origin("HTTPS://Example.org:443") == ("https", "example.org", 443)
    assert parse_origin("ftp://example.org") is None
    assert parse_origin("http://[::1]:8000") == ("http", "::1", 8000)
    assert referer_origin("http://example.org/a?b=1") == ("http", "example.org", 80)
    assert referer_origin("http://u:p@example.org/") is None
