"""Tool registry: the only way the agent touches the world.

Each tool has a pydantic argument model. Its JSON schema is what the model sees, and the same model
validates whatever the model sends back, so schema and validation can't drift apart.
Tools are read-only except propose_action, which only records a *draft* for governance to judge.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from hostops import telemetry
from hostops.agent.llm import ToolSpec
from hostops.models import ActionDraft, Category, Severity, Source
from hostops.retrieval import RunbookIndex

SUBMIT_TRIAGE = "submit_triage"


class ToolError(Exception):
    pass


class SearchRunbooksArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="What to look for, in plain words")
    k: int = Field(default=3, ge=1, le=5, description="How many runbooks to return")


class QueryMetricsArgs(BaseModel):
    name: str = Field(
        min_length=2,
        max_length=100,
        description="Substring of a hostops metric name, e.g. calendar_sync, agent_duration, http_request",
    )


class SearchLogsArgs(BaseModel):
    query: str | None = Field(default=None, max_length=200, description="Words that must all appear")
    level: str | None = Field(default=None, description="INFO, WARNING or ERROR")
    incident_id: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


class SubmitTriageArgs(BaseModel):
    category: Category
    severity: Severity
    summary: str = Field(min_length=1, max_length=1000)
    root_cause: str = Field(min_length=1, max_length=1000)
    proposed_actions: list[ActionDraft] = Field(default_factory=list, max_length=6)


@dataclass
class ToolContext:
    """Per-run state: what the tools can read, plus the drafts recorded so far."""

    index: RunbookIndex
    incident_id: str
    # Guest incidents carry untrusted text, so their runs may only read their own incident's logs.
    # Defaults to the restricted scope: forgetting to pass a source fails closed.
    source: Source = Source.GUEST
    logs: telemetry.LogBuffer = field(default_factory=lambda: telemetry.LOG_BUFFER)
    drafts: list[ActionDraft] = field(default_factory=list)


def _dump(obj: object) -> str:
    return json.dumps(obj, default=str)


def _search_runbooks(ctx: ToolContext, args: SearchRunbooksArgs) -> str:
    hits = ctx.index.search(args.query, k=args.k)
    return _dump(
        [
            {
                "id": h.runbook.id,
                "title": h.runbook.title,
                "category": h.runbook.category.value,
                "severity": h.runbook.severity.value,
                "default_actions": [a.value for a in h.runbook.default_actions],
                "score": round(h.score, 3),
                "excerpt": h.runbook.body[:700],
            }
            for h in hits
        ]
    )


def _query_metrics(ctx: ToolContext, args: QueryMetricsArgs) -> str:
    return _dump(telemetry.metric_snapshot(args.name))


def _search_logs(ctx: ToolContext, args: SearchLogsArgs) -> str:
    incident_id = ctx.incident_id if ctx.source is Source.GUEST else args.incident_id
    return _dump(ctx.logs.search(args.query, incident_id, args.level, args.limit))


def _propose_action(ctx: ToolContext, args: ActionDraft) -> str:
    ctx.drafts.append(args)
    return f"recorded draft #{len(ctx.drafts)} ({args.type.value}); governance decides after you submit"


@dataclass(frozen=True)
class _Tool:
    spec: ToolSpec
    model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], str] | None  # None for submit_triage (handled by the loop)


def _tool(
    name: str,
    description: str,
    model: type[BaseModel],
    handler: Callable[[ToolContext, BaseModel], str] | None,
) -> _Tool:
    return _Tool(ToolSpec(name, description, model.model_json_schema()), model, handler)


def _typed[A: BaseModel](
    fn: Callable[[ToolContext, A], str], model: type[A]
) -> Callable[[ToolContext, BaseModel], str]:
    def run(ctx: ToolContext, args: BaseModel) -> str:
        if not isinstance(args, model):
            raise ToolError(f"expected {model.__name__}")
        return fn(ctx, args)

    return run


TOOLS: dict[str, _Tool] = {
    t.spec.name: t
    for t in [
        _tool(
            "search_runbooks",
            "Search the house manual and ops runbooks. Returns the best matches with category, "
            "default actions and an excerpt. Start here.",
            SearchRunbooksArgs,
            _typed(_search_runbooks, SearchRunbooksArgs),
        ),
        _tool(
            "query_metrics",
            "Read current Prometheus metric values (hostops_*). Use for system incidents.",
            QueryMetricsArgs,
            _typed(_query_metrics, QueryMetricsArgs),
        ),
        _tool(
            "search_logs",
            "Search recent structured service logs by words, level or incident id.",
            SearchLogsArgs,
            _typed(_search_logs, SearchLogsArgs),
        ),
        _tool(
            "propose_action",
            "Record one proposed action. Policy decides later whether it runs, waits for approval, "
            "or is blocked.",
            ActionDraft,
            _typed(_propose_action, ActionDraft),
        ),
        _tool(
            SUBMIT_TRIAGE,
            "Finish: submit category, severity, summary, root cause and proposed actions. "
            "Call exactly once, last.",
            SubmitTriageArgs,
            None,
        ),
    ]
}

for _name in [*TOOLS, "unknown"]:  # zero baselines; see telemetry._zero_baselines
    telemetry.AGENT_TOOL_CALLS.labels(tool=_name)


def tool_specs() -> list[ToolSpec]:
    return [t.spec for t in TOOLS.values()]


def parse_args[M: BaseModel](model: type[M], args: dict[str, object]) -> M:
    try:
        return model.model_validate(args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors())
        raise ToolError(f"invalid arguments: {problems}") from exc


def run_tool(ctx: ToolContext, name: str, args: dict[str, object]) -> str:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}; available: {', '.join(TOOLS)}")
    if tool.handler is None:
        raise ToolError(f"{name} is handled by the agent loop")
    return tool.handler(ctx, parse_args(tool.model, args))
