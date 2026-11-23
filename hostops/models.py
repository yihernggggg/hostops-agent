"""Domain models. Everything that crosses a boundary (HTTP, agent tools, audit) is one of these."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field, model_validator


def _now() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class EventKind(StrEnum):
    GUEST_MESSAGE = "guest_message"
    SYSTEM_ALERT = "system_alert"


class Source(StrEnum):
    GUEST = "guest"
    SYSTEM = "system"


class Category(StrEnum):
    WIFI = "wifi"
    CHECKIN = "checkin"
    ACCESS = "access"
    APPLIANCE = "appliance"
    NOISE = "noise"
    CLEANING = "cleaning"
    BILLING = "billing"
    OTHER_GUEST = "other_guest"
    SYSTEM_CALENDAR_SYNC = "system_calendar_sync"
    SYSTEM_LLM_LATENCY = "system_llm_latency"
    SYSTEM_ERROR_RATE = "system_error_rate"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionType(StrEnum):
    NOTE = "note"
    ESCALATE_HOST = "escalate_host"
    RESTART_SYNC = "restart_sync"
    REPLY_GUEST = "reply_guest"
    DISPATCH_VENDOR = "dispatch_vendor"
    REFUND = "refund"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"  # from the agent, not yet seen by governance
    EXECUTED = "executed"  # auto-executed (low risk) or executed after approval
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    BLOCKED = "blocked"  # governance refused it outright


class IncidentStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"  # agent done, nothing waiting on a human
    AWAITING_APPROVAL = "awaiting_approval"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class Event(BaseModel):
    """Something that happened: a guest message or a system alert."""

    kind: EventKind
    text: str = Field(min_length=1, max_length=4000)
    guest_name: str | None = None
    listing_id: str | None = None
    booking_id: str | None = None
    alertname: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=_now)

    @computed_field  # type: ignore[prop-decorator]  # pydantic's documented pattern; mypy can't type it
    @property
    def source(self) -> Source:
        return Source.GUEST if self.kind is EventKind.GUEST_MESSAGE else Source.SYSTEM

    @model_validator(mode="after")
    def _alerts_have_names(self) -> Event:
        if self.kind is EventKind.SYSTEM_ALERT and not self.alertname:
            raise ValueError("system alerts need an alertname")
        return self


class ActionDraft(BaseModel):
    """What the agent may propose. Status and ids are added by the system, never by the model."""

    type: ActionType
    reason: str = Field(min_length=1, max_length=1000)
    message: str | None = Field(default=None, max_length=2000, description="Reply text or note body")
    amount_sgd: float | None = Field(default=None, ge=0, description="Refund amount in SGD")
    vendor: str | None = Field(default=None, max_length=200, description="Vendor to dispatch")


class ProposedAction(ActionDraft):
    id: str = Field(default_factory=lambda: _id("act"))
    status: ActionStatus = ActionStatus.PROPOSED
    policy_reason: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None


class TraceStep(BaseModel):
    step: int
    tool: str
    args: dict[str, object]
    observation: str


class TriageResult(BaseModel):
    category: Category
    severity: Severity
    summary: str
    root_cause: str
    proposed_actions: list[ProposedAction]
    provider: str
    steps: int
    duration_s: float
    injection_suspected: bool = False
    hit_step_cap: bool = False
    trace: list[TraceStep] = Field(default_factory=list)


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: _id("inc"))
    source: Source
    status: IncidentStatus = IncidentStatus.OPEN
    event: Event
    alertname: str | None = None
    occurrences: int = 1
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    triage: TriageResult | None = None
    actions: list[ProposedAction] = Field(default_factory=list)
