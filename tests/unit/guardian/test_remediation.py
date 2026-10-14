from africasignal.guardian.remediation import transient_fetch_error


def test_only_known_replay_safe_transport_and_server_failures_are_repairable() -> None:
    assert transient_fetch_error("FeedError: Request timed out: read deadline") == "network_timeout"
    assert (
        transient_fetch_error("NercError: Connection refused/failed: connection reset")
        == "connection_failure"
    )
    assert (
        transient_fetch_error("GdeltError: HTTP transport error: read error") == "transport_error"
    )
    assert (
        transient_fetch_error("FeedError: fetch of https://example.test returned HTTP 503")
        == "http_503"
    )
    assert transient_fetch_error("FeedError: fetch returned HTTP 429") is None
    assert transient_fetch_error("FeedError: the response is not an RSS or Atom feed") is None
    assert transient_fetch_error("NbsParseError: workbook header has changed") is None
    assert (
        transient_fetch_error("PermissionError: source permission does not allow collection")
        is None
    )
    assert transient_fetch_error("XPostOutcomeUnknown: provider may have accepted the post") is None
