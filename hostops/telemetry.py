"""Metrics, structured JSON logs, and an in-memory log buffer the agent can search.

Metrics live in a dedicated registry (not prometheus_client's global one) so tests and
multiple app instances in one process don't collide on duplicate registration.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LogRecordDict = dict[str, object]

# ---------------------------------------------------------------- metrics

REGISTRY = CollectorRegistry()

EVENTS_TOTAL = Counter("hostops_events", "Events received", ["kind"], registry=REGISTRY)
INCIDENTS_TOTAL = Counter(
    "hostops_incidents", "Incidents created (after triage)", ["source", "category"], registry=REGISTRY
)
CALENDAR_SYNC_TOTAL = Counter("hostops_calendar_sync", "Calendar sync runs", ["result"], registry=REGISTRY)
AGENT_DURATION = Histogram(
    "hostops_agent_duration_seconds",
    "Wall time of one triage run",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40),
    registry=REGISTRY,
)
AGENT_STEPS = Histogram(
    "hostops_agent_steps", "Agent loop steps per triage", buckets=(1, 2, 3, 4, 5, 6), registry=REGISTRY
)
AGENT_TOOL_CALLS = Counter("hostops_agent_tool_calls", "Tool calls by the agent", ["tool"], registry=REGISTRY)
LLM_TOKENS = Counter("hostops_llm_tokens", "LLM tokens", ["direction"], registry=REGISTRY)
LLM_ERRORS = Counter("hostops_llm_errors", "LLM provider errors", registry=REGISTRY)
PENDING_APPROVALS = Gauge("hostops_pending_approvals", "Actions waiting for a human", registry=REGISTRY)
GOVERNANCE_BLOCKS = Counter(
    "hostops_governance_blocks", "Actions blocked by policy", ["reason"], registry=REGISTRY
)
HTTP_DURATION = Histogram(
    "hostops_http_request_duration_seconds",
    "HTTP request latency",
    ["route", "status"],
    registry=REGISTRY,
)


def _zero_baselines() -> None:
    """Create every known label set at 0 so Prometheus sees a baseline before the first increment.

    Without this, a series first appears already at 1 and rate()/increase() read it as no change.
    Label sets owned by other modules (tool names, governance block reasons) are initialised there.
    """
    from hostops.models import Category, EventKind

    for kind in EventKind:
        EVENTS_TOTAL.labels(kind=kind.value)
    for category in Category:
        source = "system" if category.value.startswith("system_") else "guest"
        INCIDENTS_TOTAL.labels(source=source, category=category.value)
    for result in ("success", "failure"):
        CALENDAR_SYNC_TOTAL.labels(result=result)
    for direction in ("input", "output"):
        LLM_TOKENS.labels(direction=direction)


_zero_baselines()


def metric_snapshot(name_contains: str, limit: int = 50) -> list[LogRecordDict]:
    """Current values of hostops_* samples whose name contains the given text."""
    needle = name_contains.lower().removeprefix("hostops_")
    out: list[LogRecordDict] = []
    for family in REGISTRY.collect():
        for s in family.samples:
            if needle in s.name.lower() and not s.name.endswith("_created"):
                out.append({"name": s.name, "labels": dict(s.labels), "value": s.value})
                if len(out) >= limit:
                    return out
    return out


# ---------------------------------------------------------------- logging


class LogBuffer:
    """Bounded ring buffer of recent log records, searchable by the agent's search_logs tool."""

    def __init__(self, maxlen: int = 5000) -> None:
        self._lock = threading.Lock()
        self._records: deque[LogRecordDict] = deque(maxlen=maxlen)

    def append(self, record: LogRecordDict) -> None:
        with self._lock:
            self._records.append(record)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def search(
        self,
        query: str | None = None,
        incident_id: str | None = None,
        level: str | None = None,
        limit: int = 20,
    ) -> list[LogRecordDict]:
        terms = query.lower().split() if query else []
        with self._lock:
            records = list(self._records)
        hits = [
            r
            for r in records
            if (incident_id is None or r.get("incident_id") == incident_id)
            and (level is None or r.get("level") == level.upper())
            and all(t in json.dumps(r, default=str).lower() for t in terms)
        ]
        return hits[-limit:]


LOG_BUFFER = LogBuffer()


def _record_to_dict(record: logging.LogRecord) -> LogRecordDict:
    out: LogRecordDict = {
        "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "msg": record.getMessage(),
    }
    fields = getattr(record, "fields", None)
    if isinstance(fields, dict):
        out.update({str(k): v for k, v in fields.items()})
    if record.exc_info:
        out["exc"] = logging.Formatter().formatException(record.exc_info)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_to_dict(record), default=str)


class _BufferHandler(logging.Handler):
    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append(_record_to_dict(record))


_configured = False
_config_lock = threading.Lock()


def configure_logging(level: str = "INFO", buffer: LogBuffer = LOG_BUFFER) -> None:
    """Idempotent: JSON to stdout plus the in-memory buffer, on the `hostops` logger tree."""
    global _configured
    root = logging.getLogger("hostops")
    root.setLevel(level.upper())
    with _config_lock:
        if _configured:
            return
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(JsonFormatter())
        root.addHandler(stream)
        root.addHandler(_BufferHandler(buffer))
        root.propagate = False
        _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("hostops") else f"hostops.{name}")


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    """Structured log line. Pass incident_id=... wherever there is one."""
    logger.log(level, msg, extra={"fields": fields})
