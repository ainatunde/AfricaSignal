"""Polite, attributed HTTP fetch client. Every outbound request in AfricaSignal goes through here.

Copied from TV Insights (``app/scraping/fetch.py``); the ``curl_cffi`` branch is removed and the
per-domain rate can come from a source's ``max_requests_per_hour``.

Enforces:
- SSRF prevention: private, loopback, link-local and cloud metadata addresses are refused at every
  redirect hop, and the DNS answer is pinned at dial time.
- robots.txt and per-domain rate limits: abstains when disallowed.
- Conditional GET (RFC 9110): If-None-Match / If-Modified-Since, payload restored on 304.
- ``max_bytes`` limit on declared and streamed size.
- A deadline for the whole fetch (redirects and body), on top of the per-operation ``timeout``, so
  a server that sends one byte every few seconds cannot hold a worker for hours.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

import africasignal.net.netutil as netutil
import africasignal.net.politeness as politeness
from africasignal.net.httpcache import HttpCache
from africasignal.net.netutil import pinned_http_transport

log = logging.getLogger("africasignal.net.fetch")

REDIRECT_STATUSES = (301, 302, 303, 307, 308)  # not 304 Not Modified
MAX_REDIRECTS = 5
DEFAULT_DEADLINE_SECONDS = 60.0
_monotonic = time.monotonic  # a name of our own so tests can move the clock


@dataclass
class FetchResult:
    """Standardized result returned by ``fetch_document``."""

    url: str
    status_code: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    content: bytes = b""
    from_cache: bool = False
    abstained: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        """True strictly when the response was 200 OK without errors or abstention."""
        return self.status_code == 200 and self.error is None and not self.abstained


def _deadline_error(deadline: float) -> str:
    return f"Request timed out: the whole fetch took longer than {deadline:g} seconds"


def _cached_result(
    cache: HttpCache, url: str, current_url: str, headers: dict[str, str]
) -> FetchResult:
    cached = cache.get_cached_response(current_url) or cache.get_cached_response(url)
    if cached is None:
        return FetchResult(url=current_url, status_code=304, headers=headers, from_cache=True)
    content = cached.content
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    text = content if isinstance(content, str) else raw.decode("utf-8", errors="replace")
    return FetchResult(
        url=current_url,
        status_code=304,
        headers=headers,
        text=text,
        content=raw,
        from_cache=True,
    )


def fetch_document(
    url: str,
    user_agent: str | None = None,
    timeout: float = 15.0,
    max_bytes: int = 10_000_000,
    use_cache: bool = True,
    http_cache: HttpCache | None = None,
    max_requests_per_hour: int | None = None,
    deadline: float = DEFAULT_DEADLINE_SECONDS,
) -> FetchResult:
    """Fetch an HTTP document with SSRF protection, politeness gating, caching and size bounds.

    ``timeout`` limits each network operation; ``deadline`` limits the whole fetch. It is checked
    between redirect hops and between body chunks, so a fetch can overrun it by at most one
    ``timeout``.

    ``max_requests_per_hour`` sets the per-domain rate for this request's host (pass the source's
    ``max_requests_per_hour``); by default a domain is limited to one request per second.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return FetchResult(url=url or "", error="SSRF blocked: invalid or empty URL")

    # 1. SSRF and scheme validation
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return FetchResult(url=url, error=f"SSRF blocked: unparseable URL ({exc})")

    if parsed.scheme not in ("http", "https"):
        return FetchResult(url=url, error=f"SSRF blocked: non-http scheme '{parsed.scheme}'")

    if not netutil.is_safe_public_url(url):
        return FetchResult(
            url=url,
            error="SSRF blocked: unsafe target resolving to private, loopback, "
            "or cloud metadata address",
        )

    # 2. Politeness and robots.txt gating
    ua = user_agent or netutil.USER_AGENT
    if not politeness.politeness_gate(
        url, user_agent=ua, acquire_token=True, max_requests_per_hour=max_requests_per_hour
    ):
        log.warning("fetch_document: URL %s abstained per politeness gate.", url)
        return FetchResult(
            url=url,
            abstained=True,
            error="Politeness gate: URL disallowed by robots.txt or rate limit exceeded",
        )

    # 3. Conditional GET headers
    cache = http_cache if use_cache else None
    req_headers: dict[str, str] = {"User-Agent": ua}
    if cache is not None:
        req_headers.update(cache.get_conditional_headers(url))

    # 4. Request, following redirects by hand so every hop is checked
    started = _monotonic()

    def out_of_time() -> bool:
        return _monotonic() - started > deadline

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=pinned_http_transport(url),
        ) as client:
            current_url = url
            current_headers = dict(req_headers)
            hops = 0

            while True:
                req = client.build_request("GET", current_url, headers=current_headers)
                resp = client.send(req, stream=True)

                if resp.status_code not in REDIRECT_STATUSES:
                    break

                if out_of_time():
                    resp.close()
                    return FetchResult(url=current_url, error=_deadline_error(deadline))

                if hops >= MAX_REDIRECTS:
                    resp.close()
                    return FetchResult(
                        url=current_url,
                        status_code=resp.status_code,
                        error=f"Too many redirects (exceeded limit of {MAX_REDIRECTS})",
                    )

                location = resp.headers.get("location")
                if not location:
                    resp.close()
                    return FetchResult(
                        url=current_url,
                        status_code=resp.status_code,
                        error="Redirect response missing Location header",
                    )

                next_url = urljoin(current_url, location)
                resp.close()
                hops += 1

                try:
                    next_parsed = urlparse(next_url)
                except Exception as exc:
                    return FetchResult(
                        url=next_url, error=f"SSRF blocked: unparseable redirect URL ({exc})"
                    )

                if next_parsed.scheme not in ("http", "https"):
                    return FetchResult(
                        url=next_url,
                        error=f"SSRF blocked: non-http redirect scheme '{next_parsed.scheme}'",
                    )

                if not netutil.is_safe_public_url(next_url):
                    log.warning("fetch_document: blocked redirect to non-public host: %s", next_url)
                    return FetchResult(
                        url=next_url,
                        error="SSRF blocked: redirect to unsafe target resolving to private, "
                        "loopback, or cloud metadata address",
                    )

                if not politeness.politeness_gate(
                    next_url,
                    user_agent=ua,
                    acquire_token=True,
                    max_requests_per_hour=max_requests_per_hour,
                ):
                    log.warning("fetch_document: redirect URL %s disallowed", next_url)
                    return FetchResult(
                        url=next_url,
                        abstained=True,
                        error="Politeness gate: redirect URL disallowed by robots.txt "
                        "or rate limit exceeded",
                    )

                current_url = next_url
                current_headers = {"User-Agent": ua}
                if cache is not None:
                    current_headers.update(cache.get_conditional_headers(current_url))

            # 5. Refuse oversized bodies, by declaration first
            declared = resp.headers.get("content-length")
            if declared:
                try:
                    declared_len = int(declared)
                except ValueError:
                    declared_len = 0
                if declared_len > max_bytes:
                    resp.close()
                    return FetchResult(
                        url=current_url,
                        status_code=resp.status_code,
                        error=f"Response Content-Length {declared_len} exceeds "
                        f"max_bytes limit {max_bytes}",
                    )

            # 6. 304 Not Modified: restore the cached payload
            if resp.status_code == 304:
                resp.close()
                headers = dict(resp.headers)
                if cache is not None and (cache.is_cached(current_url) or cache.is_cached(url)):
                    return _cached_result(cache, url, current_url, headers)
                return FetchResult(
                    url=current_url, status_code=304, headers=headers, from_cache=True
                )

            # 7. Stream the body with the size limit enforced
            chunks: list[bytes] = []
            total_size = 0
            for chunk in resp.iter_bytes():
                if out_of_time():
                    resp.close()
                    return FetchResult(
                        url=current_url,
                        status_code=resp.status_code,
                        error=_deadline_error(deadline),
                    )
                total_size += len(chunk)
                if total_size > max_bytes:
                    resp.close()
                    return FetchResult(
                        url=current_url,
                        status_code=resp.status_code,
                        error=f"Response stream exceeded max_bytes limit {max_bytes}",
                    )
                chunks.append(chunk)

            resp.close()
            content = b"".join(chunks)
            text = content.decode(resp.encoding or "utf-8", errors="replace")
            resp_headers = dict(resp.headers)

            if resp.status_code == 200 and cache is not None:
                cache.store_response(current_url, 200, resp_headers, content)
            return FetchResult(
                url=current_url,
                status_code=resp.status_code,
                headers=resp_headers,
                text=text,
                content=content,
                from_cache=False,
            )

    except httpx.TimeoutException as exc:
        return FetchResult(url=url, error=f"Request timed out: {exc}")
    except httpx.ConnectError as exc:
        return FetchResult(url=url, error=f"Connection refused/failed: {exc}")
    except httpx.HTTPError as exc:
        return FetchResult(url=url, error=f"HTTP transport error: {exc}")
    except Exception as exc:
        return FetchResult(url=url, error=f"Unexpected fetch error: {exc}")
