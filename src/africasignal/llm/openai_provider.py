"""OpenAI Chat Completions JSON provider.

This adapter uses the official HTTPS endpoint, strict structured output, and the same response
accounting and local schema validation as the Anthropic adapter. No provider tools are enabled.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from africasignal.llm.adapter import ProviderResponse

ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 120.0


class OpenAIProvider:
    def __init__(
        self, api_key: str, *, client: Any | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str | None,
    ) -> ProviderResponse:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", model)[:64] or "africasignal_output"
        body: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            },
        }
        if effort is not None:
            body["reasoning_effort"] = effort
        response = self._client.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenAI returned no completion choices")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content")
        usage = payload.get("usage") or {}
        if not isinstance(text, str):
            text = ""
        reason = choice.get("finish_reason")
        stop_reason = {
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(reason, reason)
        if message.get("refusal"):
            stop_reason = "refusal"
        return ProviderResponse(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            stop_reason=stop_reason,
        )
