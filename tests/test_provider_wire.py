"""Wire-format conversion for the real providers. No network: we only build request payloads."""

import pytest

from hostops.agent.llm import AnthropicProvider, Message, OpenAICompatProvider, ToolCall

pytest.importorskip("anthropic")
pytest.importorskip("openai")

CONVO = [
    Message(role="user", content="incident"),
    Message(
        role="assistant",
        content="checking",
        tool_calls=[ToolCall("t1", "search_runbooks", {"query": "wifi"}), ToolCall("t2", "search_logs", {})],
    ),
    Message(role="tool", content="[]", tool_call_id="t1"),
    Message(role="tool", content="boom", tool_call_id="t2", is_error=True),
]


def anthropic_wire(messages: list[Message]) -> list[dict[str, object]]:
    provider = AnthropicProvider.__new__(AnthropicProvider)  # skip client construction
    return provider._to_wire(messages)


def test_anthropic_groups_parallel_tool_results_into_one_user_turn() -> None:
    wire = anthropic_wire(CONVO)
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    results = wire[2]["content"]
    assert isinstance(results, list)
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert [r["is_error"] for r in results] == [False, True]


def test_anthropic_replays_native_content_unchanged() -> None:
    native = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "t1"}]
    wire = anthropic_wire([Message(role="user", content="x"), Message(role="assistant", native=native)])
    assert wire[1]["content"] is native


def test_openai_wire_has_system_and_function_calls() -> None:
    wire = OpenAICompatProvider._to_wire("sys", CONVO)
    assert wire[0] == {"role": "system", "content": "sys"}
    calls = wire[2]["tool_calls"]
    assert isinstance(calls, list)
    assert calls[0]["function"]["name"] == "search_runbooks"
    assert [m["role"] for m in wire[3:]] == ["tool", "tool"]
