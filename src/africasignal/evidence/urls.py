"""URL canonicalisation so the same page reached by different links is one document."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_CANONICAL_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([a-zA-Z_:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _strip(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    if ":" in host:  # IPv6 literal
        netloc = f"[{host}]" + ("" if port is None else f":{port}")
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
        ]
    )
    return urlunsplit((scheme, netloc, path, query, ""))  # fragment dropped


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def declared_canonical(html: str, base_url: str) -> str | None:
    """The absolute ``<link rel="canonical">`` href, if the page declares one."""
    for tag in _CANONICAL_LINK.findall(html[:200_000]):
        attrs = {
            m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "")
            for m in _ATTR.finditer(tag)
        }
        if "canonical" in attrs.get("rel", "").lower().split() and attrs.get("href"):
            return str(urljoin(base_url, attrs["href"]))
    return None


def canonicalise(url: str, html: str | None = None) -> str:
    """Strip ``utm_*`` parameters, fragments and trailing slashes, and lowercase scheme and host.

    When ``html`` declares a ``<link rel="canonical">`` on the same site, that URL is used.
    """
    cleaned = _strip(url)
    if html:
        declared = declared_canonical(html, cleaned)
        if (
            declared
            and urlsplit(declared).scheme in ("http", "https")
            and _site(declared) == _site(cleaned)
        ):
            return _strip(declared)
    return cleaned
