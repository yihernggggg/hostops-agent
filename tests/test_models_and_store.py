import threading

import pytest
from pydantic import ValidationError

from hostops.models import (
    ActionStatus,
    ActionType,
    Category,
    Event,
    EventKind,
    IncidentStatus,
    ProposedAction,
    Source,
)
from hostops.store import IncidentStore


def guest_event(text: str = "wifi isn't working") -> Event:
    return Event(kind=EventKind.GUEST_MESSAGE, text=text, guest_name="Test Guest", listing_id="L1")


def alert_event(alertname: str = "CalendarSyncFailing") -> Event:
    return Event(kind=EventKind.SYSTEM_ALERT, text="sync failing", alertname=alertname)


def test_category_set_matches_spec() -> None:
    assert {c.value for c in Category} == {
        "wifi", "checkin", "access", "appliance", "noise", "cleaning", "billing", "other_guest",
        "system_calendar_sync", "system_llm_latency", "system_error_rate",
    }  # fmt: skip


def test_event_source_follows_kind() -> None:
    assert guest_event().source is Source.GUEST
    assert alert_event().source is Source.SYSTEM


def test_system_alert_requires_alertname() -> None:
    with pytest.raises(ValidationError):
        Event(kind=EventKind.SYSTEM_ALERT, text="no name")


def test_refund_amount_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        ProposedAction(type=ActionType.REFUND, reason="x", amount_sgd=-5)


def test_store_create_get_list() -> None:
    store = IncidentStore()
    inc = store.create(guest_event())
    assert inc.status is IncidentStatus.OPEN
    assert store.get(inc.id) is not None
    assert [i.id for i in store.list_all()] == [inc.id]
    assert store.get("missing") is None


def test_find_open_by_alertname_ignores_resolved() -> None:
    store = IncidentStore()
    inc = store.create(alert_event())
    assert store.find_open_by_alertname("CalendarSyncFailing") is not None
    store.set_status(inc.id, IncidentStatus.RESOLVED)
    assert store.find_open_by_alertname("CalendarSyncFailing") is None


def test_pending_actions_lists_only_pending() -> None:
    store = IncidentStore()
    inc = store.create(guest_event())
    pending = ProposedAction(type=ActionType.REPLY_GUEST, reason="r", status=ActionStatus.PENDING_APPROVAL)
    done = ProposedAction(type=ActionType.NOTE, reason="n", status=ActionStatus.EXECUTED)
    store.add_actions(inc.id, [pending, done])
    assert [(i, a.id) for i, a in store.pending_actions()] == [(inc.id, pending.id)]


def test_store_is_thread_safe_under_concurrent_creates() -> None:
    store = IncidentStore()
    threads = [
        threading.Thread(target=lambda: [store.create(guest_event()) for _ in range(50)]) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.list_all()) == 400
    assert len({i.id for i in store.list_all()}) == 400
