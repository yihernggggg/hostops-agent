"""Action risk policy, the refund cap, and the audit log.

The agent proposes; this module decides. It never trusts a status or amount check from the model:
every proposal is rebuilt from its draft fields and run through POLICY here, after the agent finishes.
"Executing" only logs what would happen. Nothing reaches a real guest or a real Airbnb account.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from hostops.models import ActionDraft, ActionStatus, ActionType, ProposedAction
from hostops.telemetry import GOVERNANCE_BLOCKS, get_logger, log

REFUND_CAP_SGD = 200.0

_log = get_logger("hostops.governance")


class Risk(StrEnum):
    LOW = "low"
    GUEST_FACING = "guest_facing"
    COSTS_MONEY = "costs_money"


@dataclass(frozen=True)
class Rule:
    risk: Risk
    requires_approval: bool


POLICY: dict[ActionType, Rule] = {
    ActionType.NOTE: Rule(Risk.LOW, requires_approval=False),
    ActionType.ESCALATE_HOST: Rule(Risk.LOW, requires_approval=False),
    ActionType.RESTART_SYNC: Rule(Risk.LOW, requires_approval=False),  # internal and reversible
    ActionType.REPLY_GUEST: Rule(Risk.GUEST_FACING, requires_approval=True),
    ActionType.DISPATCH_VENDOR: Rule(Risk.COSTS_MONEY, requires_approval=True),
    ActionType.REFUND: Rule(Risk.COSTS_MONEY, requires_approval=True),
}


class GovernanceError(Exception):
    pass


AuditRecord = dict[str, object]


class AuditLog:
    """Append-only JSONL. One line per tool call, proposal, decision, approval or rejection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        event: str,
        *,
        incident_id: str,
        provider: str,
        step: int | None = None,
        **data: object,
    ) -> None:
        record: AuditRecord = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "incident_id": incident_id,
            "provider": provider,
            "step": step,
            **data,
        }
        line = json.dumps(record, default=str)
        with self._lock, self.path.open("a") as fh:
            fh.write(line + "\n")

    def read(self, incident_id: str | None = None) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text().splitlines()
        records: list[AuditRecord] = [json.loads(line) for line in lines if line.strip()]
        return [r for r in records if incident_id is None or r.get("incident_id") == incident_id]


# Refunds that use up the per-incident cap. Rejected and blocked ones were never paid.
_COMMITTED = (ActionStatus.PENDING_APPROVAL, ActionStatus.EXECUTED)


def committed_refunds_sgd(
    actions: Iterable[ProposedAction], statuses: Iterable[ActionStatus] = _COMMITTED
) -> float:
    wanted = set(statuses)
    return sum(a.amount_sgd or 0.0 for a in actions if a.type is ActionType.REFUND and a.status in wanted)


def _refund_block_reason(action: ActionDraft, already_committed_sgd: float = 0.0) -> str | None:
    """The cap is per incident: this refund plus refunds already pending or paid on the incident."""
    if action.type is not ActionType.REFUND:
        return None
    if action.amount_sgd is None:
        return "refund_missing_amount"
    if already_committed_sgd + action.amount_sgd > REFUND_CAP_SGD:
        return "refund_over_cap"
    return None


class Governance:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit

    def apply(
        self,
        incident_id: str,
        provider: str,
        drafts: Sequence[ActionDraft],
        step: int | None = None,
        prior: Sequence[ProposedAction] = (),
    ) -> list[ProposedAction]:
        """Decide every proposal. Returns the resulting actions (a blocked refund yields two).

        `prior` is what the incident already has; its pending and executed refunds count toward the cap.
        """
        out: list[ProposedAction] = []
        for d in drafts:
            # Rebuild from draft fields only: any id/status the model supplied is discarded.
            action = ProposedAction.model_validate(d.model_dump(include=set(ActionDraft.model_fields)))
            self.audit.append(
                "proposal", incident_id=incident_id, provider=provider, step=step, action=action.model_dump()
            )
            block = _refund_block_reason(action, committed_refunds_sgd([*prior, *out]))
            if block:
                out.extend(self._block_refund(incident_id, provider, step, action, block))
                continue
            if POLICY[action.type].requires_approval:
                action.status = ActionStatus.PENDING_APPROVAL
                action.policy_reason = f"{POLICY[action.type].risk.value}: requires human approval"
                self._decision(incident_id, provider, step, action)
            else:
                action.policy_reason = "low risk: auto-executed"
                self._decision(incident_id, provider, step, action)
                self._execute(incident_id, provider, step, action)
            out.append(action)
        return out

    def approve(
        self,
        incident_id: str,
        action: ProposedAction,
        *,
        actor: str,
        provider: str,
        prior: Sequence[ProposedAction] = (),
    ) -> ProposedAction:
        self._require_pending(action)
        paid = committed_refunds_sgd((a for a in prior if a.id != action.id), [ActionStatus.EXECUTED])
        block = _refund_block_reason(action, paid)
        if block:  # defence in depth: never approve past the cap, counting refunds already paid
            GOVERNANCE_BLOCKS.labels(reason=block).inc()
            raise GovernanceError(f"cannot approve: {block} (cap SGD {REFUND_CAP_SGD:.0f})")
        decided = action.model_copy(
            update={"status": ActionStatus.EXECUTED, "decided_by": actor, "decided_at": datetime.now(UTC)}
        )
        self.audit.append(
            "approved", incident_id=incident_id, provider=provider, action_id=action.id, actor=actor
        )
        self._execute(incident_id, provider, None, decided)
        return decided

    def reject(
        self, incident_id: str, action: ProposedAction, *, actor: str, reason: str, provider: str
    ) -> ProposedAction:
        self._require_pending(action)
        decided = action.model_copy(
            update={"status": ActionStatus.REJECTED, "decided_by": actor, "decided_at": datetime.now(UTC)}
        )
        self.audit.append(
            "rejected",
            incident_id=incident_id,
            provider=provider,
            action_id=action.id,
            actor=actor,
            reason=reason,
        )
        return decided

    # ------------------------------------------------------------ internals

    @staticmethod
    def _require_pending(action: ProposedAction) -> None:
        if action.status is not ActionStatus.PENDING_APPROVAL:
            raise GovernanceError(f"action {action.id} is {action.status.value}, not pending_approval")

    def _block_refund(
        self, incident_id: str, provider: str, step: int | None, action: ProposedAction, reason: str
    ) -> list[ProposedAction]:
        GOVERNANCE_BLOCKS.labels(reason=reason).inc()
        action.status = ActionStatus.BLOCKED
        action.policy_reason = f"{reason}: refunds over SGD {REFUND_CAP_SGD:.0f} go to the host"
        self._decision(incident_id, provider, step, action)
        amount = "unspecified" if action.amount_sgd is None else f"SGD {action.amount_sgd:.2f}"
        escalation = ProposedAction(
            type=ActionType.ESCALATE_HOST,
            reason=(
                f"Refund of {amount} blocked by policy ({reason}); host to decide. Original: {action.reason}"
            ),
            status=ActionStatus.PROPOSED,
            policy_reason="converted from blocked refund",
        )
        self._decision(incident_id, provider, step, escalation)
        self._execute(incident_id, provider, step, escalation)
        return [action, escalation]

    def _decision(self, incident_id: str, provider: str, step: int | None, action: ProposedAction) -> None:
        decision = ActionStatus.EXECUTED if action.status is ActionStatus.PROPOSED else action.status
        self.audit.append(
            "policy_decision",
            incident_id=incident_id,
            provider=provider,
            step=step,
            action_id=action.id,
            action_type=action.type.value,
            decision=decision.value,
            policy_reason=action.policy_reason,
        )

    def _execute(self, incident_id: str, provider: str, step: int | None, action: ProposedAction) -> None:
        action.status = ActionStatus.EXECUTED
        log(
            _log,
            logging.INFO,
            f"would execute {action.type.value}",
            incident_id=incident_id,
            action_id=action.id,
            action_type=action.type.value,
            message=action.message,
            amount_sgd=action.amount_sgd,
            vendor=action.vendor,
        )
        self.audit.append(
            "action_executed",
            incident_id=incident_id,
            provider=provider,
            step=step,
            action_id=action.id,
            action_type=action.type.value,
            simulated=True,
        )
