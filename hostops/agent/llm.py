"""Provider interface plus the real providers (Anthropic, OpenAI-compatible such as Ollama).

The loop speaks one small, provider-neutral message format. Each provider translates it to its own
wire format. Assistant turns also keep the provider's *native* content (`Message.native`) so it can be
replayed unchanged: Claude's thinking blocks must be sent back exactly as received.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, cast

from hostops.chaos import ChaosState

if TYPE_CHECKING:
    from anthropic.types.beta import BetaMessageParam, BetaToolUnionParam
    from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam

    from hostops.config import Settings


Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, object]


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    native: object | None = None  # provider-native assistant content, replayed as-is


@dataclass(frozen=True)
class LLMResponse:
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    native: object | None = None


class ProviderError(Exception):
    """Anything that stops the provider producing a usable step (network, auth, refusal, truncation)."""


class LLMProvider(Protocol):
    name: str

    def next_step(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...


class ChaosLatency:
    """Wraps a provider and adds the chaos-configured delay, to rehearse the AgentLatencyHigh alert."""

    def __init__(self, inner: LLMProvider, chaos: ChaosState) -> None:
        self.inner = inner
        self.chaos = chaos
        self.name = inner.name

    def next_step(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        delay = self.chaos.snapshot().llm_latency_s
        if delay:
            time.sleep(delay)
        return self.inner.next_step(system, messages, tools)


def _as_args(raw: object) -> dict[str, object]:
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else {}
    if not isinstance(raw, dict):
        raise ProviderError(f"tool input is not an object: {type(raw).__name__}")
    return {str(k): v for k, v in raw.items()}


# ---------------------------------------------------------------- Anthropic


class AnthropicProvider:
    """Claude via the official SDK, manual tool-use loop (the loop lives in loop.py, not here)."""

    name = "anthropic"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model: str, timeout_s: float, effort: Effort = "medium") -> None:
        import anthropic  # optional extra; imported lazily so offline runs need no SDK

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=timeout_s, max_retries=2)
        self.model = model
        self.effort = effort

    def _to_wire(self, messages: Sequence[Message]) -> list[dict[str, object]]:
        wire: list[dict[str, object]] = []
        pending_results: list[dict[str, object]] = []

        def flush() -> None:
            if pending_results:  # all tool results for one turn go in a single user message
                wire.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for m in messages:
            if m.role == "tool":
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content,
                        "is_error": m.is_error,
                    }
                )
                continue
            flush()
            if m.role == "assistant":
                content: object = m.native if m.native is not None else self._assistant_blocks(m)
                wire.append({"role": "assistant", "content": content})
            else:
                wire.append({"role": "user", "content": m.content})
        flush()
        return wire

    @staticmethod
    def _assistant_blocks(m: Message) -> list[dict[str, object]]:
        blocks: list[dict[str, object]] = [{"type": "text", "text": m.content}] if m.content else []
        blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.tool_calls]
        return blocks

    def next_step(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        a = self._anthropic
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                # Our wire dicts follow the SDK's TypedDict shapes; cast so mypy checks the rest of the call.
                messages=cast("list[BetaMessageParam]", self._to_wire(messages)),
                tools=cast(
                    "list[BetaToolUnionParam]",
                    [
                        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                        for t in tools
                    ],
                ),
                tool_choice={"type": "auto"},  # forced tool_choice is rejected on current models
                output_config={"effort": self.effort},
                betas=[self.FALLBACK_BETA],
                fallbacks="default",  # on a policy refusal the API re-runs on a fallback model
            )
        except (a.APIConnectionError, a.APIStatusError) as exc:
            raise ProviderError(f"anthropic: {exc}") from exc

        if response.stop_reason == "refusal":
            raise ProviderError("anthropic: request refused")
        if response.stop_reason == "max_tokens":
            raise ProviderError("anthropic: output truncated at max_tokens")

        calls: list[ToolCall] = []
        texts: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, args=_as_args(block.input)))
            elif block.type == "text":
                texts.append(block.text)
        return LLMResponse(
            tool_calls=calls,
            text="\n".join(texts),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            native=response.content,
        )


# ---------------------------------------------------------------- OpenAI-compatible (Ollama, vLLM, ...)


class OpenAICompatProvider:
    name = "openai"

    def __init__(self, base_url: str, model: str, timeout_s: float) -> None:
        import openai  # optional extra

        self._openai = openai
        # Local servers like Ollama ignore the key, but the SDK insists on one being set.
        self._client = openai.OpenAI(base_url=base_url, api_key="unused-local", timeout=timeout_s)
        self.model = model

    @staticmethod
    def _to_wire(system: str, messages: Sequence[Message]) -> list[dict[str, object]]:
        wire: list[dict[str, object]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "tool":
                wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            elif m.role == "assistant":
                entry: dict[str, object] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.args)},
                        }
                        for c in m.tool_calls
                    ]
                wire.append(entry)
            else:
                wire.append({"role": "user", "content": m.content})
        return wire

    def next_step(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        o = self._openai
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=cast("list[ChatCompletionMessageParam]", self._to_wire(system, messages)),
                tools=cast(
                    "list[ChatCompletionToolUnionParam]",
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": t.description,
                                "parameters": t.input_schema,
                            },
                        }
                        for t in tools
                    ],
                ),
                tool_choice="auto",
            )
        except (o.APIConnectionError, o.APIStatusError) as exc:
            raise ProviderError(f"openai-compatible: {exc}") from exc
        if not response.choices:
            raise ProviderError("openai-compatible: empty response")
        msg = response.choices[0].message
        try:
            calls = [
                ToolCall(id=c.id, name=c.function.name, args=_as_args(c.function.arguments))
                for c in (msg.tool_calls or [])
                if c.type == "function"
            ]
        except json.JSONDecodeError as exc:
            raise ProviderError(f"openai-compatible: bad tool arguments: {exc}") from exc
        usage = response.usage
        return LLMResponse(
            tool_calls=calls,
            text=msg.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


def make_provider(settings: Settings, chaos: ChaosState) -> LLMProvider:
    from hostops.agent.offline import OfflineProvider

    inner: LLMProvider
    if settings.provider == "anthropic":
        inner = AnthropicProvider(settings.anthropic_model, settings.llm_timeout_s, settings.anthropic_effort)
    elif settings.provider == "openai":
        inner = OpenAICompatProvider(settings.openai_base_url, settings.openai_model, settings.llm_timeout_s)
    else:
        inner = OfflineProvider()
    return ChaosLatency(inner, chaos)
