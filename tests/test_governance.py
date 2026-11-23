import json
from pathlib import Path

import pytest

from hostops import telemetry
from hostops.governance import (
    POLICY,
    REFUND_CAP_SGD,
    AuditLog,
    Governance,
    GovernanceError,
    Risk,
)
from hostops.models import ActionDraft, ActionStatus, ActionType, ProposedAction


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def gov(audit: AuditLog) -> Governance:
    return Governance(audit)


def draft(type_: ActionType, **kw: object) -> ActionDraft:
    return ActionDraft.model_validate({"type": type_, "reason": "test", **kw})


def apply_one(gov: Governance, d: ActionDraft) -> list[ProposedAction]:
    return gov.apply("inc_1", "offline", [d], step=3)


def test_policy_table_matches_claude_md() -> None:
    assert {t: (r.risk, r.requires_approval) for t, r in POLICY.items()} == {
        ActionType.NOTE: (Risk.LOW, False),
        ActionType.ESCALATE_HOST: (Risk.LOW, False),
        ActionType.RESTART_SYNC: (Risk.LOW, False),
        ActionType.REPLY_GUEST: (Risk.GUEST_FACING, True),
        ActionType.DISPATCH_VENDOR: (Risk.COSTS_MONEY, True),
        ActionType.REFUND: (Risk.COSTS_MONEY, True),
    }
    assert REFUND_CAP_SGD == 200


@pytest.mark.parametrize("t", [ActionType.NOTE, ActionType.ESCALATE_HOST, ActionType.RESTART_SYNC])
def test_low_risk_actions_auto_execute(gov: Governance, t: ActionType) -> None:
    [action] = apply_one(gov, draft(t))
    assert action.status is ActionStatus.EXECUTED


@pytest.mark.parametrize(
    "d",
    [
        draft(ActionType.REPLY_GUEST, message="hi"),
        draft(ActionType.DISPATCH_VENDOR, vendor="CoolFix"),
        draft(ActionType.REFUND, amount_sgd=50),
        draft(ActionType.REFUND, amount_sgd=REFUND_CAP_SGD),  # at the cap is allowed, over is not
    ],
)
def test_guest_facing_and_costly_actions_wait_for_approval(gov: Governance, d: ActionDraft) -> None:
    [action] = apply_one(gov, d)
    assert action.status is ActionStatus.PENDING_APPROVAL


def test_status_claimed_by_the_model_is_ignored(gov: Governance) -> None:
    sneaky = ProposedAction(type=ActionType.REPLY_GUEST, reason="r", status=ActionStatus.EXECUTED)
    [action] = apply_one(gov, sneaky)
    assert action.status is ActionStatus.PENDING_APPROVAL


def test_refund_over_cap_is_blocked_and_converted_to_escalation(gov: Governance) -> None:
    before = telemetry.REGISTRY.get_sample_value(
        "hostops_governance_blocks_total", {"reason": "refund_over_cap"}
    )
    blocked, escalation = apply_one(gov, draft(ActionType.REFUND, amount_sgd=250))
    assert blocked.type is ActionType.REFUND
    assert blocked.status is ActionStatus.BLOCKED
    assert escalation.type is ActionType.ESCALATE_HOST
    assert escalation.status is ActionStatus.EXECUTED
    assert "250" in escalation.reason
    after = telemetry.REGISTRY.get_sample_value(
        "hostops_governance_blocks_total", {"reason": "refund_over_cap"}
    )
    assert (after or 0) == (before or 0) + 1


def test_refund_without_amount_fails_closed(gov: Governance) -> None:
    blocked, escalation = apply_one(gov, draft(ActionType.REFUND))
    assert blocked.status is ActionStatus.BLOCKED
    assert escalation.type is ActionType.ESCALATE_HOST


def test_every_decision_is_audited_with_context(gov: Governance, audit: AuditLog) -> None:
    apply_one(gov, draft(ActionType.REPLY_GUEST, message="hi"))
    records = audit.read()
    decision = next(r for r in records if r["event"] == "policy_decision")
    assert decision["incident_id"] == "inc_1"
    assert decision["provider"] == "offline"
    assert decision["step"] == 3
    assert decision["decision"] == "pending_approval"
    assert "ts" in decision


def test_audit_file_is_valid_jsonl(gov: Governance, audit: AuditLog) -> None:
    apply_one(gov, draft(ActionType.NOTE))
    lines = audit.path.read_text().splitlines()
    assert lines
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_approve_executes_and_records_who(gov: Governance, audit: AuditLog) -> None:
    [pending] = apply_one(gov, draft(ActionType.REPLY_GUEST, message="hi"))
    approved = gov.approve("inc_1", pending, actor="host@example.test", provider="offline")
    assert approved.status is ActionStatus.EXECUTED
    assert approved.decided_by == "host@example.test"
    assert approved.decided_at is not None
    events = [r["event"] for r in audit.read()]
    assert "approved" in events
    assert "action_executed" in events


def test_reject_records_reason(gov: Governance, audit: AuditLog) -> None:
    [pending] = apply_one(gov, draft(ActionType.DISPATCH_VENDOR, vendor="CoolFix"))
    rejected = gov.reject("inc_1", pending, actor="host", reason="guest fixed it", provider="offline")
    assert rejected.status is ActionStatus.REJECTED
    assert any(r["event"] == "rejected" and r["reason"] == "guest fixed it" for r in audit.read())


@pytest.mark.parametrize("status", [ActionStatus.EXECUTED, ActionStatus.BLOCKED, ActionStatus.REJECTED])
def test_only_pending_actions_can_be_decided(gov: Governance, status: ActionStatus) -> None:
    action = ProposedAction(type=ActionType.REFUND, reason="r", amount_sgd=10, status=status)
    with pytest.raises(GovernanceError):
        gov.approve("inc_1", action, actor="host", provider="offline")
    with pytest.raises(GovernanceError):
        gov.reject("inc_1", action, actor="host", reason="x", provider="offline")


def test_approval_rechecks_the_cap(gov: Governance) -> None:
    # Defence in depth: even if a pending refund were edited above the cap, approval must refuse it.
    over = ProposedAction(
        type=ActionType.REFUND, reason="r", amount_sgd=999, status=ActionStatus.PENDING_APPROVAL
    )
    with pytest.raises(GovernanceError, match="cap"):
        gov.approve("inc_1", over, actor="host", provider="offline")
