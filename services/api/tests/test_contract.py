"""Route contract tests for the FastAPI adapter.

These tests exercise the adapter without starting a real server: they use the
FastAPI `TestClient` and a temporary `ConsoleApp` root.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Point the app at a temporary workspace, then construct a client."""
    monkeypatch.setitem(app.state.console_config, "storage_root", str(tmp_path))
    # Force a fresh app instance for each test to pick up the new config.
    app.state.console = None
    app.state.console_api = None
    with TestClient(app) as c:
        yield c


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_preflight_reports_workspace(client):
    response = client.get("/api/preflight")
    assert response.status_code == 200
    data = response.json()
    assert "preflight" in data
    assert "checks" in data["preflight"]


def test_create_project_contract(client):
    response = client.post("/api/projects", json={
        "title": "Test Projesi",
        "operator_name": "API Operator",
    })
    assert response.status_code == 201
    data = response.json()
    assert data["project"]["title"] == "Test Projesi"
    assert data["project"]["operator_name"] == "API Operator"


def test_ingest_routes_exist_and_are_guarded(client):
    # Without a project or rights decision, the request is not blocked by the
    # route table; the catalog error tells the caller what is missing.
    response = client.post("/api/projects/prj_dummy/rights", json={
        "candidate_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "license_type": "fair_use_claim",
        "decided_by": "nur",
        "risk_acknowledged": True,
    })
    assert response.status_code in (404, 422)

    response = client.post("/api/projects/prj_dummy/ingest", json={
        "candidate_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    })
    assert response.status_code in (404, 422)


def test_publish_routes_exist_and_are_guarded(client):
    # The route exists, but the project does not (or preconditions fail).
    response = client.get("/api/projects/prj_dummy/publish/state")
    assert response.status_code in (404, 422)
