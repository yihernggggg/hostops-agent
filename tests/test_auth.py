from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from hostops.config import Settings
from hostops.service import OPEN_PATHS, create_app

TOKEN = "test-token-not-secret"  # noqa: S105  # test fixture value


def make_app(tmp_path: Path, **overrides: object):
    settings = Settings.model_validate(
        {"audit_path": tmp_path / "audit.jsonl", "sync_interval_s": 0, "api_token": TOKEN, **overrides}
    )
    return create_app(settings)


@pytest.fixture
def anon(tmp_path: Path) -> TestClient:
    return TestClient(make_app(tmp_path))


def test_health_is_open(anon: TestClient) -> None:
    assert anon.get("/healthz").status_code == 200


def test_missing_token_is_401_with_bearer_challenge(anon: TestClient) -> None:
    r = anon.get("/incidents")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_401(anon: TestClient) -> None:
    assert anon.get("/incidents", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert anon.get("/incidents", headers={"Authorization": TOKEN}).status_code == 401  # scheme required


def test_right_token_is_allowed(anon: TestClient) -> None:
    assert anon.get("/incidents", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_every_route_except_open_paths_requires_the_token(tmp_path: Path) -> None:
    # Guards new endpoints: adding a route without auth fails here.
    app = make_app(tmp_path)
    client = TestClient(app)
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path not in OPEN_PATHS]
    assert len(routes) >= 12
    for route in routes:
        path = route.path.replace("{incident_id}", "inc_x").replace("{action_id}", "act_x")
        for method in route.methods:
            assert client.request(method, path).status_code == 401, f"{method} {route.path}"
    assert {"/healthz"} == set(OPEN_PATHS)


def test_service_refuses_to_start_without_a_token(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="HOSTOPS_API_TOKEN"):
        make_app(tmp_path, api_token=None)


def test_token_can_come_from_a_file(tmp_path: Path) -> None:
    token_file = tmp_path / "api_token"
    token_file.write_text("from-file-token\n")
    client = TestClient(make_app(tmp_path, api_token=None, api_token_file=token_file))
    assert client.get("/incidents", headers={"Authorization": "Bearer from-file-token"}).status_code == 200


def test_auth_can_only_be_disabled_explicitly(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path, api_token=None, auth_disabled=True))
    assert client.get("/incidents").status_code == 200
