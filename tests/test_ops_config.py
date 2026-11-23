"""Checks the monitoring config without running Docker.

A typo in a PromQL metric name fails silently: the panel is empty and the alert never fires.
These tests make that a red build instead.
"""

import json
import re
from pathlib import Path

import pytest

from hostops import telemetry
from hostops.models import Category

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops"
RULES = OPS / "prometheus" / "alert_rules.yml"
PROM = OPS / "prometheus" / "prometheus.yml"
ALERTMANAGER = OPS / "alertmanager" / "alertmanager.yml"
DASHBOARD = OPS / "grafana" / "dashboards" / "hostops.json"
DATASOURCE = OPS / "grafana" / "provisioning" / "datasources" / "prometheus.yml"

METRIC = re.compile(r"\bhostops_[a-z_]+\b")
SUFFIXES = {"counter": ["_total"], "histogram": ["_bucket", "_count", "_sum"], "gauge": [""]}


def exported_series() -> set[str]:
    names: set[str] = set()
    for family in telemetry.REGISTRY.collect():
        names.update(family.name + s for s in SUFFIXES[family.type])
    return names


def dashboard() -> dict[str, object]:
    data = json.loads(DASHBOARD.read_text())
    assert isinstance(data, dict)
    return data


def panels() -> list[dict[str, object]]:
    raw = dashboard()["panels"]
    assert isinstance(raw, list)
    return raw


def dashboard_exprs() -> list[str]:
    exprs = []
    for p in panels():
        for t in p.get("targets", []):
            exprs.append(t["expr"])
    return exprs


def alert_blocks() -> list[str]:
    return re.split(r"\n\s*- alert: ", RULES.read_text())[1:]


@pytest.mark.parametrize("path", [RULES, DASHBOARD])
def test_every_referenced_metric_is_exported(path: Path) -> None:
    referenced = set(METRIC.findall(path.read_text()))
    assert referenced, f"no hostops_ metrics found in {path.name}"
    assert referenced - exported_series() == set()


def test_alert_rules_match_claude_md() -> None:
    names = {block.split("\n", 1)[0].strip() for block in alert_blocks()}
    assert names == {"CalendarSyncFailing", "AgentLatencyHigh", "ErrorRateHigh", "PendingApprovalsHigh"}


def test_every_alert_routes_to_a_real_category() -> None:
    for block in alert_blocks():
        match = re.search(r"category:\s*(\S+)", block)
        assert match, block
        assert match.group(1) in {c.value for c in Category}, block


def test_prometheus_loads_the_rules_and_scrapes_the_service() -> None:
    text = PROM.read_text()
    assert "alert_rules.yml" in text
    assert "hostops:8000" in text
    assert "alertmanager:9093" in text


def test_alertmanager_posts_to_the_alerts_webhook_and_sends_resolved() -> None:
    text = ALERTMANAGER.read_text()
    assert "http://hostops:8000/alerts" in text
    assert re.search(r"send_resolved:\s*true", text)


def test_dashboard_panels_are_well_formed() -> None:
    ids = [p["id"] for p in panels()]
    assert len(ids) == len(set(ids))
    uid = re.search(r"uid:\s*(\S+)", DATASOURCE.read_text())
    assert uid
    for p in panels():
        if p.get("type") == "row":
            continue
        assert p.get("title"), p
        assert p.get("datasource", {}).get("uid") == uid.group(1), p["title"]
        assert p.get("targets"), p["title"]
    assert len(dashboard_exprs()) >= 8


def test_compose_publishes_ports_on_loopback_only() -> None:
    # Grafana uses admin/admin and Prometheus/Alertmanager have no auth: they stay on loopback, always.
    # Only the API may be rebound (HOSTOPS_BIND, used on EC2 behind a security group), and its default
    # is still loopback.
    ports = re.findall(r'^\s*-\s*"([^"]+:\d+)"\s*$', (ROOT / "docker-compose.yml").read_text(), re.M)
    assert len(ports) == 4
    api = [p for p in ports if p.endswith(":8000")]
    others = [p for p in ports if not p.endswith(":8000")]
    assert api == ["${HOSTOPS_BIND:-127.0.0.1}:${HOSTOPS_PORT:-8000}:8000"]
    assert all(p.startswith("127.0.0.1:") for p in others), others


def test_compose_image_is_overridable_for_deploys() -> None:
    assert "image: ${HOSTOPS_IMAGE:-hostops-agent:local}" in (ROOT / "docker-compose.yml").read_text()


def test_alertmanager_repeats_often_enough_to_recover_lost_incidents() -> None:
    # The incident store is in memory: after a service restart, a still-firing alert only comes back
    # when Alertmanager repeats it. Repeats merge into the open incident, so frequent is safe.
    match = re.search(r"repeat_interval:\s*(\d+)([smh])", ALERTMANAGER.read_text())
    assert match
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    assert seconds <= 300


def test_prometheus_and_alertmanager_authenticate_to_the_api() -> None:
    assert re.search(r"credentials_file:\s*/run/secrets/api_token", PROM.read_text())
    assert re.search(r"credentials_file:\s*/run/secrets/api_token", ALERTMANAGER.read_text())


def test_compose_passes_the_token_as_a_secret_file() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "HOSTOPS_API_TOKEN_FILE: /run/secrets/api_token" in compose
    assert re.search(r"^secrets:\s*\n\s+api_token:\s*\n\s+file:", compose, re.M)
    assert "secrets/" in (ROOT / ".gitignore").read_text().splitlines()
