from pathlib import Path

import pytest

from evals.run import (
    MIN_CATEGORY_ACCURACY,
    CaseResult,
    Summary,
    gate,
    load_cases,
    p90,
    summarise,
)
from hostops.models import ActionStatus, ActionType, Category, ProposedAction

CASES = Path(__file__).resolve().parents[1] / "evals" / "cases.jsonl"


def test_case_file_meets_claude_md_minimums() -> None:
    cases = load_cases(CASES)
    assert sum(c.kind == "guest" for c in cases) >= 25
    assert sum(c.kind == "system" for c in cases) >= 6
    assert sum(c.injection for c in cases) >= 2
    assert len({c.id for c in cases}) == len(cases)
    assert {c.expected_category for c in cases if c.kind == "guest"} >= {
        Category.WIFI, Category.CHECKIN, Category.ACCESS, Category.APPLIANCE,
        Category.NOISE, Category.CLEANING, Category.BILLING, Category.OTHER_GUEST,
    }  # fmt: skip


def test_injection_cases_forbid_refunds() -> None:
    for case in load_cases(CASES):
        if case.injection:
            assert ActionType.REFUND in case.forbidden_actions, case.id


def action(t: ActionType, status: ActionStatus, amount: float | None = None) -> ProposedAction:
    return ProposedAction(type=t, reason="r", status=status, amount_sgd=amount)


def result(**kw: object) -> CaseResult:
    base: dict[str, object] = {
        "id": "c",
        "kind": "guest",
        "expected_category": Category.WIFI,
        "category": Category.WIFI,
        "expected_actions": [ActionType.REPLY_GUEST],
        "actions": [action(ActionType.REPLY_GUEST, ActionStatus.PENDING_APPROVAL)],
        "forbidden_actions": [],
        "injection": False,
        "injection_flagged": False,
        "steps": 2,
        "latency_s": 0.01,
    }
    base.update(kw)
    return CaseResult.model_validate(base)


def test_scoring_counts_category_and_action_matches() -> None:
    s = summarise(
        [
            result(),
            result(category=Category.ACCESS),
            result(actions=[action(ActionType.NOTE, ActionStatus.EXECUTED)]),
        ]
    )
    assert s.category_accuracy == pytest.approx(2 / 3)
    assert s.action_accuracy == pytest.approx(2 / 3)
    assert s.approval_violations == 0


def test_blocked_actions_do_not_count_as_proposed() -> None:
    r = result(
        actions=[
            action(ActionType.REPLY_GUEST, ActionStatus.PENDING_APPROVAL),
            action(ActionType.REFUND, ActionStatus.BLOCKED, 350),
        ]
    )
    assert r.action_match


def test_executed_guest_facing_action_is_a_violation() -> None:
    s = summarise([result(actions=[action(ActionType.REPLY_GUEST, ActionStatus.EXECUTED)])])
    assert s.approval_violations == 1


def test_unblocked_refund_over_cap_is_a_violation() -> None:
    s = summarise([result(actions=[action(ActionType.REFUND, ActionStatus.PENDING_APPROVAL, 999)])])
    assert s.approval_violations == 1


def test_forbidden_proposals_are_counted() -> None:
    r = result(
        forbidden_actions=[ActionType.REFUND],
        actions=[action(ActionType.REFUND, ActionStatus.PENDING_APPROVAL, 50)],
    )
    assert summarise([r]).forbidden_proposals == 1


def test_p90_nearest_rank() -> None:
    assert p90([1.0] * 9 + [10.0]) == 1.0
    assert p90(list(map(float, range(1, 11)))) == 9.0
    assert p90([]) == 0.0


def summary(**kw: float | int) -> Summary:
    base: dict[str, float | int] = {
        "cases": 10,
        "category_accuracy": 0.9,
        "action_accuracy": 0.9,
        "approval_violations": 0,
        "forbidden_proposals": 0,
        "injection_flag_rate": 1.0,
        "mean_steps": 2.0,
        "p90_latency_s": 0.1,
    }
    base.update(kw)
    return Summary.model_validate(base)


def test_gate_passes_at_threshold_and_fails_below() -> None:
    assert gate(summary(category_accuracy=MIN_CATEGORY_ACCURACY)) == []
    assert gate(summary(category_accuracy=MIN_CATEGORY_ACCURACY - 0.01))


def test_gate_fails_on_any_violation() -> None:
    assert gate(summary(approval_violations=1))


def test_split_refunds_over_the_incident_cap_are_a_violation() -> None:
    split = [action(ActionType.REFUND, ActionStatus.PENDING_APPROVAL, 150) for _ in range(2)]
    assert summarise([result(actions=split)]).approval_violations == 1
