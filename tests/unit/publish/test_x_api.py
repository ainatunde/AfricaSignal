from __future__ import annotations

from typing import Any

import httpx
import pytest

from africasignal.publish import x_api


class Response:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Client:
    def __init__(self, response: Response | Exception):
        self.response = response

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, *args: Any, **kwargs: Any) -> Response:
        if isinstance(self.response, Exception):
            raise self.response
        self.call = (args, kwargs)
        return self.response

    def post(self, *args: Any, **kwargs: Any) -> Response:
        if isinstance(self.response, Exception):
            raise self.response
        self.call = (args, kwargs)
        return self.response


def test_create_post_sends_text_to_x_v2_and_returns_post_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(Response(201, {"data": {"id": "123456789"}}))
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: client)

    receipt = x_api.create_post("user-token", "Verified update")

    assert receipt.post_id == "123456789"
    args, kwargs = client.call
    assert args == (x_api.POSTS_URL,)
    assert kwargs["headers"] == {"Authorization": "Bearer user-token"}
    assert kwargs["json"] == {"text": "Verified update"}


def test_create_reply_binds_the_exact_summoned_post(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(Response(201, {"data": {"id": "987654321"}}))
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: client)

    receipt = x_api.create_reply("user-token", "Evidence link", "123456789")

    assert receipt.post_id == "987654321"
    args, kwargs = client.call
    assert args == (x_api.POSTS_URL,)
    assert kwargs["json"] == {
        "text": "Evidence link",
        "reply": {"in_reply_to_tweet_id": "123456789"},
    }


def test_create_reply_rejects_a_non_numeric_target() -> None:
    with pytest.raises(x_api.XPostRejected):
        x_api.create_reply("user-token", "text", "not-a-post")


def test_authenticated_user_lookup_returns_only_the_numeric_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(Response(200, {"data": {"id": "2244994945", "name": "not retained"}}))
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: client)

    assert x_api.authenticated_user_id("user-token") == "2244994945"
    args, _kwargs = client.call
    assert args == (f"{x_api.USERS_URL}/me",)


def test_mentions_read_discards_text_and_requests_only_minimal_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(
        Response(
            200,
            {
                "data": [
                    {
                        "id": "123456789",
                        "author_id": "44",
                        "conversation_id": "123456789",
                        "created_at": "2026-10-14T10:00:00Z",
                        "text": "This content must not be retained",
                    }
                ],
                "meta": {"newest_id": "123456789", "next_token": "page-2"},
            },
        )
    )
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: client)

    result = x_api.get_mentions("user-token", "2244994945", 20, "100")

    assert result.fetched_count == 1
    assert result.newest_id == "123456789"
    assert result.next_token == "page-2"
    assert result.mentions[0].post_id == "123456789"
    assert result.mentions[0].author_id == "44"
    assert result.mentions[0].conversation_id == "123456789"
    assert result.mentions[0].created_at is not None
    assert not hasattr(result.mentions[0], "text")
    args, kwargs = client.call
    assert args == (f"{x_api.USERS_URL}/2244994945/mentions",)
    assert kwargs["params"]["post.fields"] == "id,conversation_id,author_id,created_at"
    assert "text" not in kwargs["params"]["post.fields"]


def test_mentions_read_uses_the_saved_page_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(Response(200, {"data": [], "meta": {"newest_id": "200"}}))
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: client)

    x_api.get_mentions("user-token", "2244994945", 20, "100", "page-2")

    assert client.call[1]["params"]["since_id"] == "100"
    assert client.call[1]["params"]["pagination_token"] == "page-2"


def test_create_post_classifies_client_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: Client(Response(403, {})))
    with pytest.raises(x_api.XPostRejected):
        x_api.create_post("user-token", "text")


@pytest.mark.parametrize(
    "response",
    [
        Response(503, {}),
        Response(200, {"data": {"id": "not-a-number"}}),
        Response(201, ValueError()),
    ],
)
def test_create_post_never_retries_ambiguous_outcomes(
    monkeypatch: pytest.MonkeyPatch, response: Response
) -> None:
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: Client(response))
    with pytest.raises(x_api.XPostOutcomeUnknown):
        x_api.create_post("user-token", "text")


def test_transport_failure_is_an_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    error = httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(x_api.httpx, "Client", lambda **kwargs: Client(error))
    with pytest.raises(x_api.XPostOutcomeUnknown):
        x_api.create_post("user-token", "text")
