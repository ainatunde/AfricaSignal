import pytest

from africasignal.evidence.urls import canonicalise


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.NG/News/A?utm_source=x&utm_medium=y", "https://example.ng/News/A"),
        ("https://example.ng/a#comments", "https://example.ng/a"),
        ("https://example.ng/a/", "https://example.ng/a"),
        ("https://example.ng/a///", "https://example.ng/a"),
        ("https://example.ng", "https://example.ng/"),
        ("https://example.ng/", "https://example.ng/"),
        ("HTTPS://EXAMPLE.NG/a", "https://example.ng/a"),
        ("https://example.ng:443/a", "https://example.ng/a"),
        ("http://example.ng:80/a", "http://example.ng/a"),
        ("http://example.ng:8080/a", "http://example.ng:8080/a"),
        ("https://example.ng/a?id=7&UTM_Campaign=z", "https://example.ng/a?id=7"),
        ("https://example.ng/a?id=7&page=2", "https://example.ng/a?id=7&page=2"),
        ("https://example.ng/a?q=", "https://example.ng/a?q="),
        ("  https://example.ng/a  ", "https://example.ng/a"),
    ],
)
def test_canonicalise(raw: str, expected: str) -> None:
    assert canonicalise(raw) == expected


def test_same_page_reached_by_different_links_is_one_url() -> None:
    variants = [
        "https://example.ng/a/?utm_source=whatsapp",
        "https://example.ng/a#top",
        "https://EXAMPLE.ng/a",
    ]
    assert len({canonicalise(v) for v in variants}) == 1


def test_declared_canonical_on_same_site_is_used() -> None:
    html = '<html><head><link rel="canonical" href="https://example.ng/news/petrol-price/"></head></html>'
    assert canonicalise("https://example.ng/amp/petrol?utm_x=1", html) == (
        "https://example.ng/news/petrol-price"
    )


def test_declared_canonical_relative_href_and_www_are_handled() -> None:
    html = "<link href='/story/1' rel='canonical'>"
    assert (
        canonicalise("https://www.example.ng/m/story/1", html) == "https://www.example.ng/story/1"
    )
    html = '<link rel="canonical" href="https://example.ng/story/1">'
    assert canonicalise("https://www.example.ng/m/1", html) == "https://example.ng/story/1"


def test_declared_canonical_on_another_site_is_ignored() -> None:
    html = '<link rel="canonical" href="https://evil.example/steal">'
    assert canonicalise("https://example.ng/a", html) == "https://example.ng/a"


def test_non_http_canonical_is_ignored() -> None:
    html = '<link rel="canonical" href="javascript:alert(1)">'
    assert canonicalise("https://example.ng/a", html) == "https://example.ng/a"


def test_other_link_rels_are_not_canonical() -> None:
    html = '<link rel="stylesheet" href="https://example.ng/x.css"><link rel="alternate" href="/a">'
    assert canonicalise("https://example.ng/a", html) == "https://example.ng/a"
