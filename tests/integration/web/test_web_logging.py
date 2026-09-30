"""A reader's request leaves no log line with their address or the URL they asked for (AS-043
gap G5). Covers the application's own logging, including its error paths."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

BROWSER = {"User-Agent": "Mozilla/5.0"}


def test_requests_write_no_address_or_query_string_to_any_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client.get("/explore?q=secret-search-text&ref=x", headers=BROWSER)
    client.get("/no-such-page?token=secret-search-text", headers=BROWSER)
    client.post(
        "/places/locate",
        json={"lat": 6.5244, "lon": 3.3792},
        headers={**BROWSER, "Origin": "https://evil.example"},  # the refusal path logs too
    )
    client.post("/signin", data={"email": "reader@example.org"}, headers=BROWSER)
    # The test client is itself an httpx client and logs its own requests; the server side is
    # everything else.
    text = "\n".join(
        record.getMessage()
        for record in caplog.records
        if not record.name.startswith(("httpx", "httpcore"))
    )
    assert text, "the refused POST should have been logged, or this test checks nothing"
    for forbidden in ("secret-search-text", "testclient", "127.0.0.1", "6.5244", "3.3792"):
        assert forbidden not in text
