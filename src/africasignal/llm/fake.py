"""A provider for tests: answers from a script, never touches the network (AS-020)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from africasignal.llm.adapter import ProviderResponse


@dataclass(frozen=True)
class FakeRequest:
    model: str
    system: str
    user: str
    schema: dict[str, Any]
    max_tokens: int
    effort: str | None


@dataclass(frozen=True)
class FakeReply:
    """Full control over one answer. A plain object or string returned instead is wrapped with
    token counts estimated from the text (four characters a token)."""

    text: str
    input_tokens: int = 100
    output_tokens: int = 50
    stop_reason: str | None = "end_turn"


Script = Callable[[FakeRequest], Any] | list[Any] | dict[str, Any] | str


@dataclass
class FakeProvider:
    """``script`` is a function of the request, a list of answers used in order, or one constant
    answer. An answer is a ``FakeReply``, a string (used as the raw text) or any JSON value."""

    script: Script
    calls: list[FakeRequest] = field(default_factory=list)

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
        request = FakeRequest(model, system, user, schema, max_tokens, effort)
        self.calls.append(request)
        if callable(self.script):
            answer = self.script(request)
        elif isinstance(self.script, list):
            if len(self.calls) > len(self.script):
                raise AssertionError("FakeProvider script ran out of answers")
            answer = self.script[len(self.calls) - 1]
        else:
            answer = self.script
        if not isinstance(answer, FakeReply):
            text = answer if isinstance(answer, str) else json.dumps(answer)
            answer = FakeReply(
                text,
                input_tokens=max(1, (len(system) + len(user)) // 4),
                output_tokens=max(1, len(text) // 4),
            )
        return ProviderResponse(
            text=answer.text,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            stop_reason=answer.stop_reason,
        )


def reply_from_recording(path: Path) -> FakeReply:
    """The answer saved by ``python -m africasignal.llm.record``, for a FakeProvider to replay."""
    recording = json.loads(path.read_text())
    return FakeReply(
        text=recording["response_text"],
        input_tokens=recording["usage"]["input_tokens"],
        output_tokens=recording["usage"]["output_tokens"],
        stop_reason=recording.get("stop_reason", "end_turn"),
    )
