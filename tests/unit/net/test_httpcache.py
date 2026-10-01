from africasignal.net.httpcache import HttpCache


def test_uncached_url_has_no_conditional_headers() -> None:
    cache = HttpCache()
    assert cache.get_conditional_headers("https://example.com/feed.xml") == {}
    assert cache.is_cached("https://example.com/feed.xml") is False


def test_conditional_headers_carry_etag_and_last_modified() -> None:
    cache = HttpCache()
    url = "https://example.com/epg"
    cache.store_response(
        url,
        200,
        {"ETag": '"abc"', "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
        "<tv/>",
    )
    assert cache.is_cached(url)
    assert cache.get_conditional_headers(url) == {
        "If-None-Match": '"abc"',
        "If-Modified-Since": "Wed, 21 Oct 2026 07:28:00 GMT",
    }


def test_header_names_are_case_insensitive() -> None:
    cache = HttpCache()
    url = "https://example.com/h2-feed"
    cache.store_response(
        url, 200, {"etag": '"e"', "last-modified": "Thu, 22 Oct 2026 12:00:00 GMT"}, "OK"
    )
    headers = cache.get_conditional_headers(url)
    assert headers["If-None-Match"] == '"e"'
    assert headers["If-Modified-Since"] == "Thu, 22 Oct 2026 12:00:00 GMT"


def test_text_and_binary_payloads_round_trip() -> None:
    cache = HttpCache()
    cache.store_response("https://example.com/a", 200, {}, "<html/>")
    cache.store_response("https://example.com/b", 200, {}, b"\x1f\x8b\x08data")
    a = cache.get_cached_response("https://example.com/a")
    b = cache.get_cached_response("https://example.com/b")
    assert a is not None and a.content == "<html/>"
    assert b is not None and b.content == b"\x1f\x8b\x08data"


def test_only_200_is_stored() -> None:
    cache = HttpCache()
    for status in (304, 404, 500):
        cache.store_response(f"https://example.com/{status}", status, {"ETag": "x"}, "body")
        assert cache.is_cached(f"https://example.com/{status}") is False
        assert cache.get_cached_response(f"https://example.com/{status}") is None


def test_invalidate_and_clear() -> None:
    cache = HttpCache()
    cache.store_response("https://example.com/a", 200, {}, "a")
    cache.store_response("https://example.com/b", 200, {}, "b")
    cache.invalidate("https://example.com/a")
    assert not cache.is_cached("https://example.com/a") and cache.is_cached("https://example.com/b")
    cache.clear()
    assert not cache.is_cached("https://example.com/b")
