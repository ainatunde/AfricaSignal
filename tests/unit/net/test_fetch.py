import socket

import httpx
import pytest

from africasignal.net import fetch as fetch_mod
from africasignal.net import netutil, politeness
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.net.httpcache import HttpCache

from .conftest import ChunkedStream, FakeWeb

URL = "https://news.example.ng/article"


# --- FetchResult --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "abstained", "ok"),
    [
        (200, None, False, True),
        (200, "boom", False, False),
        (200, None, True, False),
        (404, None, False, False),
        (304, None, False, False),
        (0, None, False, False),
    ],
)
def test_success_only_for_clean_200(
    status: int, error: str | None, abstained: bool, ok: bool
) -> None:
    assert FetchResult(url="u", status_code=status, error=error, abstained=abstained).success is ok


# --- SSRF: refused before any request is sent ------------------------------------------------


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to send a request or open a socket fails the test."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted for a refused URL")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://127.0.0.1:8080/metrics",
        "http://localhost:8000/secrets",
        "http://10.0.0.1/internal",
        "http://172.16.0.1/backend",
        "http://192.168.1.1/router",
        "http://[::1]/admin",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials",
        # Hostnames whose DNS answer is private (see DNS_TABLE in conftest).
        "http://internal.example/",
        "http://metadata.example/latest/meta-data",
        "http://loopback.example/",
        "http://v6-local.example/",
        # One private address among public ones is enough to refuse.
        "http://mixed.example/",
    ],
)
def test_private_targets_are_refused_without_network_io(url: str, no_network: None) -> None:
    result = fetch_document(url)
    assert result.success is False
    assert result.status_code == 0
    assert result.abstained is False
    assert result.error is not None and "ssrf" in result.error.lower()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://mirror.example/file.txt",
        "gopher://127.0.0.1:6379/_",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "not-a-valid-url",
        "",
        "   ",
    ],
)
def test_non_http_and_malformed_urls_are_refused(url: str, no_network: None) -> None:
    result = fetch_document(url)
    assert result.success is False and result.error is not None and result.status_code == 0


def test_redirect_to_private_ip_is_refused(web: FakeWeb) -> None:
    web.redirect(URL, "http://127.0.0.1:8080/admin")
    result = fetch_document(URL)
    assert result.success is False
    assert result.error is not None and "ssrf" in result.error.lower()
    assert result.url == "http://127.0.0.1:8080/admin"
    assert web.fetched() == [URL]  # the private target was never requested


def test_redirect_to_cloud_metadata_is_refused(web: FakeWeb) -> None:
    web.redirect(URL, "http://169.254.169.254/latest/meta-data")
    result = fetch_document(URL)
    assert result.error is not None and "ssrf" in result.error.lower()
    assert web.fetched() == [URL]


def test_redirect_to_hostname_resolving_to_private_ip_is_refused(web: FakeWeb) -> None:
    web.redirect(URL, "https://internal.example/secret")
    result = fetch_document(URL)
    assert result.error is not None and "ssrf" in result.error.lower()
    assert web.fetched() == [URL]


def test_redirect_to_non_http_scheme_is_refused(web: FakeWeb) -> None:
    web.redirect(URL, "file:///etc/passwd")
    result = fetch_document(URL)
    assert result.error is not None and "scheme" in result.error.lower()


def test_safe_redirect_is_followed(web: FakeWeb) -> None:
    web.redirect("https://example.ng/old", "https://example.ng/new", status=301)
    web.add("https://example.ng/new", content=b"Target payload")
    result = fetch_document("https://example.ng/old")
    assert result.success and result.content == b"Target payload"
    assert result.url == "https://example.ng/new"


def test_redirect_loop_stops_after_five_hops(web: FakeWeb) -> None:
    web.redirect("https://example.ng/a", "https://example.ng/b")
    web.redirect("https://example.ng/b", "https://example.ng/a")
    result = fetch_document("https://example.ng/a")
    assert result.error is not None and "too many redirects" in result.error.lower()
    assert len(web.fetched()) == 6  # the first request plus five hops


def test_redirect_without_location_is_an_error(web: FakeWeb) -> None:
    web.add(URL, status=302)
    assert "Location" in (fetch_document(URL).error or "")


# --- politeness ---------------------------------------------------------------------------------


def test_robots_disallow_is_honoured_without_fetching_the_page(web: FakeWeb) -> None:
    web.add("https://news.example.ng/robots.txt", content=b"User-agent: *\nDisallow: /article\n")
    web.add(URL, content=b"secret")
    result = fetch_document(URL)
    assert result.abstained is True and result.success is False
    assert web.fetched() == []


def test_robots_rules_for_our_own_agent_apply(web: FakeWeb) -> None:
    web.add(
        "https://news.example.ng/robots.txt",
        content=b"User-agent: AfricaSignalBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
    )
    web.add(URL, content=b"page")
    assert fetch_document(URL).abstained is True


def test_missing_robots_txt_allows_fetching(web: FakeWeb) -> None:
    web.add(URL, content=b"page")
    assert fetch_document(URL).success is True


def test_robots_fetch_failure_fails_open(web: FakeWeb) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("robots unreachable")

    web.add("https://news.example.ng/robots.txt", handler=boom)
    web.add(URL, content=b"page")
    assert fetch_document(URL).success is True


def test_rate_limit_abstains(web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        politeness, "rate_limiter", politeness.TokenBucketRateLimiter(rate=0.001, capacity=1)
    )
    web.add(URL, content=b"page")
    assert fetch_document(URL).success is True
    second = fetch_document(URL)
    assert second.abstained is True and "rate limit" in (second.error or "")


def test_source_budget_sets_the_domain_rate(web: FakeWeb) -> None:
    web.add(URL, content=b"page")
    results = [fetch_document(URL, max_requests_per_hour=60) for _ in range(7)]
    assert [r.success for r in results] == [True] * 5 + [False] * 2
    assert all(r.abstained for r in results[5:])


def test_user_agent_identifies_the_bot(web: FakeWeb) -> None:
    web.add(URL, content=b"page")
    fetch_document(URL)
    ua = web.header(URL, "user-agent")
    assert ua is not None and ua.startswith("AfricaSignalBot/1.0 (+") and "/about/bot" in ua


def test_custom_user_agent_is_used_for_robots_and_request(web: FakeWeb) -> None:
    web.add(URL, content=b"page")
    fetch_document(URL, user_agent="TestBot/9")
    assert web.header(URL, "user-agent") == "TestBot/9"


# --- caching --------------------------------------------------------------------------------------


def test_fresh_200_is_cached(web: FakeWeb) -> None:
    web.add(
        URL,
        headers={"ETag": '"v1"', "Last-Modified": "Wed, 16 Sep 2026 12:00:00 GMT"},
        content=b"{}",
    )
    cache = HttpCache()
    result = fetch_document(URL, http_cache=cache)
    assert result.success and result.from_cache is False
    entry = cache.get_cached_response(URL)
    assert entry is not None and entry.etag == '"v1"'


def test_conditional_headers_are_sent_for_cached_urls(web: FakeWeb) -> None:
    cache = HttpCache()
    cache.store_response(
        URL, 200, {"ETag": '"e9"', "Last-Modified": "Sun, 01 Mar 2026 00:00:00 GMT"}, "old"
    )
    web.add(URL, content=b"new")
    fetch_document(URL, http_cache=cache)
    assert web.header(URL, "if-none-match") == '"e9"'
    assert web.header(URL, "if-modified-since") == "Sun, 01 Mar 2026 00:00:00 GMT"


def test_304_restores_the_cached_payload(web: FakeWeb) -> None:
    cache = HttpCache()
    cache.store_response(URL, 200, {"ETag": '"e"'}, b"cached body")
    web.add(URL, status=304)
    result = fetch_document(URL, http_cache=cache)
    assert result.status_code == 304 and result.from_cache is True
    assert result.content == b"cached body" and result.text == "cached body"
    assert result.success is False  # 304 is not a fresh 200


def test_304_without_a_cache_entry_has_no_payload(web: FakeWeb) -> None:
    web.add(URL, status=304)
    result = fetch_document(URL, http_cache=HttpCache())
    assert result.status_code == 304 and result.content == b"" and result.from_cache is True


def test_cache_is_updated_when_content_changes(web: FakeWeb) -> None:
    cache = HttpCache()
    cache.store_response(URL, 200, {"ETag": '"old"'}, b"old")
    web.add(URL, headers={"ETag": '"new"'}, content=b"new")
    assert fetch_document(URL, http_cache=cache).content == b"new"
    entry = cache.get_cached_response(URL)
    assert entry is not None and entry.etag == '"new"' and entry.content == b"new"


def test_use_cache_false_ignores_the_cache(web: FakeWeb) -> None:
    cache = HttpCache()
    cache.store_response(URL, 200, {"ETag": '"e"'}, b"old")
    web.add(URL, headers={"ETag": '"n"'}, content=b"new")
    fetch_document(URL, http_cache=cache, use_cache=False)
    assert web.header(URL, "if-none-match") is None
    entry = cache.get_cached_response(URL)
    assert entry is not None and entry.content == b"old"  # untouched


def test_error_responses_are_not_cached(web: FakeWeb) -> None:
    cache = HttpCache()
    web.add(URL, status=500, content=b"Server Error")
    result = fetch_document(URL, http_cache=cache)
    assert result.status_code == 500 and result.success is False
    assert cache.is_cached(URL) is False


# --- size limits ---------------------------------------------------------------------------


def test_declared_content_length_over_limit_is_refused(web: FakeWeb) -> None:
    web.add(URL, headers={"Content-Length": "52428800"}, content=b"never-read")
    result = fetch_document(URL, max_bytes=1000)
    assert result.success is False and "max_bytes" in (result.error or "")


def test_streamed_body_over_limit_is_cut_off(web: FakeWeb) -> None:
    web.add(URL, stream=ChunkedStream([b"a" * 200, b"b" * 200, b"c" * 200]))
    result = fetch_document(URL, max_bytes=250)
    assert result.success is False and "max_bytes" in (result.error or "")
    assert result.content == b""


def test_payload_within_limit_is_returned(web: FakeWeb) -> None:
    web.add(URL, headers={"Content-Length": "25"}, content=b"Permitted size content OK")
    result = fetch_document(URL, max_bytes=1000)
    assert result.success and result.content == b"Permitted size content OK"


def test_body_exactly_at_limit_is_allowed(web: FakeWeb) -> None:
    web.add(URL, content=b"x" * 100)
    assert fetch_document(URL, max_bytes=100).success is True


# --- failures never raise ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "text"),
    [
        (httpx.ConnectTimeout("slow"), "timed out"),
        (httpx.ConnectError("refused"), "connection"),
        (httpx.ReadError("reset"), "transport error"),
        (RuntimeError("weird"), "unexpected"),
    ],
)
def test_transport_failures_become_error_results(web: FakeWeb, exc: Exception, text: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    web.add(URL, handler=boom)
    result = fetch_document(URL)
    assert result.success is False and text in (result.error or "").lower()


def test_http_error_statuses_are_returned_not_raised(web: FakeWeb) -> None:
    for status in (403, 404, 429, 503):
        web.add(URL, status=status, content=b"err")
        result = fetch_document(URL)
        assert result.status_code == status and result.success is False and result.error is None


def test_all_outbound_requests_use_the_pinned_transport(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch_document`` builds its client from ``pinned_http_transport`` and never follows
    redirects itself."""
    seen: dict[str, object] = {}
    real_client = httpx.Client

    def spy(*args: object, **kwargs: object) -> httpx.Client:
        seen.update(kwargs)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fetch_mod.httpx, "Client", spy)
    web.add(URL, content=b"page")
    fetch_document(URL)
    assert isinstance(seen["transport"], httpx.MockTransport)
    assert seen["follow_redirects"] is False and seen["trust_env"] is False


def test_module_exposes_the_bot_user_agent() -> None:
    assert netutil.USER_AGENT.startswith("AfricaSignalBot/1.0 (+https://")
