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
