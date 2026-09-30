import json
from types import SimpleNamespace
from typing import Any

from africasignal.llm.anthropic_provider import AnthropicProvider
from africasignal.llm.fake import FakeProvider, FakeReply


class StubMessages:
    def __init__(self, message: Any) -> None:
        self.message = message
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.message


def stub_client(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    message = SimpleNamespace(
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=321, output_tokens=45),
        stop_reason=stop_reason,
    )
    return SimpleNamespace(messages=StubMessages(message))


def test_anthropic_request_asks_for_a_json_schema_and_sends_no_tools() -> None:
    client = stub_client('{"ok": true}')
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    reply = AnthropicProvider(client=client).complete(
        model="claude-sonnet-5-5",
        system="sys",
        user="usr",
        schema=schema,
        max_tokens=500,
        effort="low",
    )
    sent = client.messages.kwargs
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["max_tokens"] == 500
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "usr"}]
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": schema},
        "effort": "low",
    }
    assert "tools" not in sent and "tool_choice" not in sent
    assert (reply.text, reply.input_tokens, reply.output_tokens) == ('{"ok": true}', 321, 45)


def test_anthropic_request_omits_effort_when_not_configured() -> None:
    client = stub_client("{}")
    AnthropicProvider(client=client).complete(
        model="m", system="s", user="u", schema={"type": "object"}, max_tokens=1, effort=None
    )
    assert "effort" not in client.messages.kwargs["output_config"]


def test_anthropic_passes_the_stop_reason_through() -> None:
    reply = AnthropicProvider(client=stub_client("", "refusal")).complete(
        model="m", system="s", user="u", schema={"type": "object"}, max_tokens=1, effort=None
    )
    assert reply.stop_reason == "refusal"


def test_fake_provider_records_requests_and_uses_a_list_in_order() -> None:
    fake = FakeProvider([{"n": 1}, FakeReply("raw", input_tokens=7, output_tokens=3)])
    a = fake.complete(model="m", system="s", user="u", schema={}, max_tokens=9, effort=None)
    b = fake.complete(model="m", system="s", user="u2", schema={}, max_tokens=9, effort="low")
    assert json.loads(a.text) == {"n": 1}
    assert (b.text, b.input_tokens, b.output_tokens) == ("raw", 7, 3)
    assert [c.user for c in fake.calls] == ["u", "u2"]
