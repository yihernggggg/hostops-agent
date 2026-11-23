"""The agent loop: build messages, ask the provider for the next step, run tools, append observations, repeat.

Written by hand (no framework) so every step can be explained:

    messages = [incident rendered as untrusted data]
    for step in 1..MAX_STEPS:
        response = provider.next_step(system, messages, tools)
        if no tool calls: nudge and continue
        for each tool call: run it (or accept submit_triage), append the observation
        if submit_triage was accepted: done
    hit the cap -> escalate to the host

The loop never executes actions. It returns proposals; governance decides in pipeline.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from hostops.agent.injection import looks_like_injection
from hostops.agent.llm import LLMProvider, Message, ProviderError
from hostops.agent.prompt import SYSTEM_PROMPT, render_incident
from hostops.agent.tools import (
    SUBMIT_TRIAGE,
    TOOLS,
    SubmitTriageArgs,
    ToolContext,
    ToolError,
    parse_args,
    run_tool,
    tool_specs,
)
from hostops.governance import AuditLog
from hostops.models import (
    ActionDraft,
    ActionType,
    Category,
    Incident,
    ProposedAction,
    Severity,
    Source,
    TraceStep,
    TriageResult,
)
from hostops.telemetry import AGENT_DURATION, AGENT_STEPS, AGENT_TOOL_CALLS, LLM_ERRORS, LLM_TOKENS

__all__ = ["DEFAULT_MAX_STEPS", "TriageAgent", "render_incident"]

DEFAULT_MAX_STEPS = 6
MAX_OBSERVATION_CHARS = 4000
NUDGE = "Use a tool to investigate, or call submit_triage to finish."


class TriageAgent:
    def __init__(
        self,
        provider: LLMProvider,
        context_factory: Callable[[Incident], ToolContext],
        audit: AuditLog | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.provider = provider
        self.context_factory = context_factory
        self.audit = audit
        self.max_steps = max_steps

    def run(self, incident: Incident) -> TriageResult:
        start = time.perf_counter()
        injection = incident.source is Source.GUEST and looks_like_injection(incident.event.text)
        ctx = self.context_factory(incident)
        messages = [Message(role="user", content=render_incident(incident, injection_suspected=injection))]
        trace: list[TraceStep] = []
        specs = tool_specs()

        for step in range(1, self.max_steps + 1):
            try:
                response = self.provider.next_step(SYSTEM_PROMPT, messages, specs)
            except ProviderError as exc:
                LLM_ERRORS.inc()
                self._audit("llm_error", incident, step, error=str(exc))
                return self._escalate(
                    incident, ctx, trace, step, start, injection, f"LLM provider failed: {exc}"
                )

            LLM_TOKENS.labels(direction="input").inc(response.input_tokens)
            LLM_TOKENS.labels(direction="output").inc(response.output_tokens)
            self._audit(
                "llm_step",
                incident,
                step,
                tool_calls=[c.name for c in response.tool_calls],
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
            messages.append(
                Message(
                    role="assistant",
                    content=response.text,
                    tool_calls=response.tool_calls,
                    native=response.native,
                )
            )
            if not response.tool_calls:
                messages.append(Message(role="user", content=NUDGE))
                continue

            submitted: SubmitTriageArgs | None = None
            for call in response.tool_calls:
                AGENT_TOOL_CALLS.labels(tool=call.name if call.name in TOOLS else "unknown").inc()
                self._audit("tool_call", incident, step, tool=call.name, args=call.args)
                is_error = False
                try:
                    if call.name == SUBMIT_TRIAGE:
                        submitted = parse_args(SubmitTriageArgs, call.args)
                        observation = "triage accepted"
                    else:
                        observation = run_tool(ctx, call.name, call.args)
                except ToolError as exc:
                    observation, is_error = f"error: {exc}", True
                observation = observation[:MAX_OBSERVATION_CHARS]
                trace.append(TraceStep(step=step, tool=call.name, args=call.args, observation=observation))
                messages.append(
                    Message(role="tool", content=observation, tool_call_id=call.id, is_error=is_error)
                )
            if submitted is not None:
                return self._finish(
                    incident,
                    category=submitted.category,
                    severity=submitted.severity,
                    summary=submitted.summary,
                    root_cause=submitted.root_cause,
                    drafts=[*ctx.drafts, *submitted.proposed_actions],
                    trace=trace,
                    steps=step,
                    start=start,
                    injection=injection,
                )

        return self._escalate(
            incident,
            ctx,
            trace,
            self.max_steps,
            start,
            injection,
            f"hit the {self.max_steps}-step cap",
            cap=True,
        )

    # ------------------------------------------------------------ helpers

    def _escalate(
        self,
        incident: Incident,
        ctx: ToolContext,
        trace: list[TraceStep],
        steps: int,
        start: float,
        injection: bool,
        why: str,
        cap: bool = False,
    ) -> TriageResult:
        """Fail safe: whatever went wrong, a human hears about it. Partial drafts are discarded."""
        hint = incident.event.labels.get("category", "")
        fallback = Category.OTHER_GUEST if incident.source is Source.GUEST else Category.SYSTEM_ERROR_RATE
        category = Category(hint) if hint in {c.value for c in Category} else fallback
        return self._finish(
            incident,
            category=category,
            severity=Severity.HIGH,
            summary=f"Agent could not finish triage ({why}); escalated to host.",
            root_cause="unknown: agent did not complete",
            drafts=[ActionDraft(type=ActionType.ESCALATE_HOST, reason=f"Agent escalation: {why}")],
            trace=trace,
            steps=steps,
            start=start,
            injection=injection,
            cap=cap,
        )

    def _finish(
        self,
        incident: Incident,
        *,
        category: Category,
        severity: Severity,
        summary: str,
        root_cause: str,
        drafts: list[ActionDraft],
        trace: list[TraceStep],
        steps: int,
        start: float,
        injection: bool,
        cap: bool = False,
    ) -> TriageResult:
        if injection and not any(d.type is ActionType.NOTE for d in drafts):
            drafts = [
                *drafts,
                ActionDraft(
                    type=ActionType.NOTE,
                    reason="Possible prompt injection in guest message",
                    message="Pre-screen flagged this message. Review before approving anything.",
                ),
            ]
        duration = time.perf_counter() - start
        AGENT_DURATION.labels(provider=self.provider.name).observe(duration)
        AGENT_STEPS.observe(steps)
        result = TriageResult(
            category=category,
            severity=severity,
            summary=summary,
            root_cause=root_cause,
            proposed_actions=[ProposedAction.model_validate(d.model_dump()) for d in drafts],
            provider=self.provider.name,
            steps=steps,
            duration_s=duration,
            injection_suspected=injection,
            hit_step_cap=cap,
            trace=trace,
        )
        self._audit(
            "agent_finished",
            incident,
            steps,
            category=category.value,
            severity=severity.value,
            hit_step_cap=cap,
            injection_suspected=injection,
            duration_s=round(duration, 4),
        )
        return result

    def _audit(self, event: str, incident: Incident, step: int, **data: object) -> None:
        if self.audit is not None:
            self.audit.append(event, incident_id=incident.id, provider=self.provider.name, step=step, **data)
