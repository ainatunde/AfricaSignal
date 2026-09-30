"""The Anthropic provider: one Messages API call that must answer in JSON (AS-020).

This is the one place the plan allows an outbound call that does not go through
``africasignal.net.fetch.fetch_document``: the request goes to the Anthropic API through the
official SDK, carries only text the caller built, and gives the model no tools.

Structured output is requested with ``output_config.format`` (a JSON schema). Claude Sonnet 5.5 and
Claude Opus 5.5 reject forced ``tool_choice``, so the tool-use route is not available.

NOT YET CHECKED AGAINST THE LIVE API: no API key exists in the build environment. The request
shape follows the SDK documentation and is covered by tests against a stub client only. Run
``python -m africasignal.llm.record`` with a key to exercise it.
"""

from __future__ import annotations

from typing import Any

import anthropic

from africasignal.llm.adapter import ProviderResponse

DEFAULT_TIMEOUT_SECONDS = 120.0


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # ``client`` lets tests pass a stub; the SDK retries 408/409/429/5xx itself (2 retries).
        self._client: Any = client or anthropic.Anthropic(api_key=api_key, timeout=timeout)

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
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        message = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        text = next((b.text for b in message.content if b.type == "text"), "")
        return ProviderResponse(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
        )
