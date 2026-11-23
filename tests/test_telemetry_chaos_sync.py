import json
import logging
import random

import pytest
from pydantic import ValidationError

from hostops import telemetry
from hostops.calendar_sync import CalendarSync
from hostops.chaos import ChaosSettings, ChaosState


@pytest.fixture(autouse=True)
def _clean_logs() -> None:
    telemetry.configure_logging("DEBUG")
    telemetry.LOG_BUFFER.clear()


def sample(name: str, **labels: str) -> float:
    value = telemetry.REGISTRY.get_sample_value(name, labels or None)
    return value or 0.0


# ---------------------------------------------------------------- logging


def test_json_formatter_emits_fields_and_incident_id() -> None:
    record = logging.LogRecord("hostops.x", logging.INFO, __file__, 1, "hello", None, None)
    record.fields = {"incident_id": "inc_1", "n": 3}
    out = json.loads(telemetry.JsonFormatter().format(record))
    assert out["msg"] == "hello"
    assert out["level"] == "INFO"
    assert out["incident_id"] == "inc_1"
    assert out["n"] == 3
    assert "ts" in out


def test_log_buffer_search_filters_by_incident_and_text() -> None:
    log = telemetry.get_logger("hostops.test")
    telemetry.log(log, logging.INFO, "sync failed for listing", incident_id="inc_a")
    telemetry.log(log, logging.INFO, "sync ok", incident_id="inc_b")
    telemetry.log(log, logging.ERROR, "unrelated failure")

    assert [r["incident_id"] for r in telemetry.LOG_BUFFER.search(incident_id="inc_a")] == ["inc_a"]
    assert len(telemetry.LOG_BUFFER.search(query="sync")) == 2
    assert [r["msg"] for r in telemetry.LOG_BUFFER.search(level="ERROR")] == ["unrelated failure"]


def test_log_buffer_is_bounded() -> None:
    buf = telemetry.LogBuffer(maxlen=3)
    for i in range(5):
        buf.append({"msg": str(i), "level": "INFO"})
    assert [r["msg"] for r in buf.search(limit=10)] == ["2", "3", "4"]


def test_metric_snapshot_finds_hostops_samples() -> None:
    telemetry.EVENTS_TOTAL.labels(kind="guest_message").inc()
    snap = telemetry.metric_snapshot("events_total")
    assert any(s["name"] == "hostops_events_total" and s["labels"] == {"kind": "guest_message"} for s in snap)


# ---------------------------------------------------------------- chaos


def test_chaos_defaults_off_and_reset() -> None:
    chaos = ChaosState()
    assert chaos.snapshot() == ChaosSettings()
    chaos.update(calendar_sync_failing=True, llm_latency_s=2.0)
    assert chaos.snapshot().calendar_sync_failing is True
    chaos.reset()
    assert chaos.snapshot() == ChaosSettings()


def test_chaos_validates_ranges() -> None:
    with pytest.raises(ValidationError):
        ChaosState().update(http_error_rate=1.5)


def test_http_error_injection_respects_rate() -> None:
    chaos = ChaosState()
    rng = random.Random(1)
    assert not any(chaos.should_fail_http(rng) for _ in range(100))
    chaos.update(http_error_rate=1.0)
    assert all(chaos.should_fail_http(rng) for _ in range(100))


# ---------------------------------------------------------------- calendar sync


def test_calendar_sync_success_and_failure_are_counted_and_logged() -> None:
    chaos = ChaosState()
    sync = CalendarSync(chaos)
    ok_before = sample("hostops_calendar_sync_total", result="success")
    fail_before = sample("hostops_calendar_sync_total", result="failure")

    assert sync.run().ok is True
    chaos.update(calendar_sync_failing=True)
    failed = sync.run()

    assert failed.ok is False
    assert failed.error
    assert sample("hostops_calendar_sync_total", result="success") == ok_before + 1
    assert sample("hostops_calendar_sync_total", result="failure") == fail_before + 1
    assert telemetry.LOG_BUFFER.search(query="calendar sync failed", level="ERROR")
