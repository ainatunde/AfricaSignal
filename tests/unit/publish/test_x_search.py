from __future__ import annotations

from typing import Any

import pytest

from africasignal.publish import x_search


class Response:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> Any:
        return self.payload


class Client:
    def __init__(self, response: Response):
        self.response = response

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, *args: Any, **kwargs: Any) -> Response:
        self.call = (args, kwargs)
        return self.response


def test_recent_search_requests_a_bounded_page_and_returns_ids_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(
        Response(
            200,
            {
                "data": [
                    {"id": "2", "text": "discard this"},
                    {"id": "3", "text": "discard this too"},
                ],
                "meta": {"newest_id": "3"},
            },
        )
    )
    monkeypatch.setattr(x_search.httpx, "Client", lambda **kwargs: client)

    result = x_search.search_recent("app-token", "fuel Lagos", 20, "1")

    assert result.post_ids == ("2", "3")
    assert result.fetched_count == 2
    assert result.newest_id == "3"
    args, kwargs = client.call
    assert args == (x_search.SEARCH_URL,)
    assert kwargs["headers"] == {"Authorization": "Bearer app-token"}
    assert kwargs["params"] == {"query": "fuel Lagos", "max_results": "20", "since_id": "1"}
    assert not hasattr(result, "text")


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"id": "bad"}]},
        {"data": [{"id": "1"}, {"id": "2"}, {"id": "3"}]},
        {"data": [], "meta": {"newest_id": "bad"}},
    ],
)
def test_invalid_search_responses_are_unknown_outcomes(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    monkeypatch.setattr(x_search.httpx, "Client", lambda **kwargs: Client(Response(200, payload)))
    with pytest.raises(x_search.XSearchOutcomeUnknown):
        x_search.search_recent("app-token", "fuel", 2, None)


def test_recent_search_classifies_definitive_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x_search.httpx, "Client", lambda **kwargs: Client(Response(429, {})))
    with pytest.raises(x_search.XSearchRejected):
        x_search.search_recent("app-token", "fuel", 10, None)
