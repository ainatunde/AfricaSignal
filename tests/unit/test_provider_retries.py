"""Exercise the actual SDK retry boundary with an offline HTTP transport."""

from functools import partial

import anthropic
import httpx2 as httpx
import pytest

from africasignal.llm import anthropic_provider


def test_transient_provider_error_makes_only_one_billed_request(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            503, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            anthropic_provider.anthropic,
            "Anthropic",
            partial(anthropic.Anthropic, http_client=client),
        )
        provider = anthropic_provider.AnthropicProvider(api_key="offline-test-key")
        with pytest.raises(anthropic.APIStatusError):
            provider.complete(
                model="anthropic/test-model",
                system="system",
                user="user",
                schema={"type": "object"},
                max_tokens=10,
                effort=None,
            )
    assert len(requests) == 1
