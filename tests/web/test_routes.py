"""Tests for the FastAPI web dashboard API.

These tests build a FastAPI app over an isolated temporary state directory and
exercise the REST routes via the FastAPI ``TestClient``. They are skipped
gracefully if the optional ``web`` extra (FastAPI + httpx) is not installed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from qracer.alerts import AlertCondition  # noqa: E402
from qracer.web.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient bound to a fresh, isolated qracer state directory."""
    app = create_app(user_dir=tmp_path)
    with TestClient(app) as client:
        yield client


class TestHealth:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestAlertRoutes:
    def test_list_alerts_empty(self, client: TestClient) -> None:
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        assert resp.json() == {"alerts": []}

    def test_create_and_list_alert(self, client: TestClient) -> None:
        body = {"ticker": "AAPL", "condition": "above", "threshold": 200.0}
        resp = client.post("/api/alerts", json=body)
        assert resp.status_code == 201
        alert = resp.json()["alert"]
        assert alert["ticker"] == "AAPL"
        assert alert["condition"] == "above"
        assert alert["threshold"] == 200.0
        assert alert["active"] is True

        listing = client.get("/api/alerts").json()
        assert len(listing["alerts"]) == 1
        assert listing["alerts"][0]["id"] == alert["id"]

    def test_create_alert_validation_error(self, client: TestClient) -> None:
        # Missing required fields
        resp = client.post("/api/alerts", json={"ticker": "AAPL"})
        assert resp.status_code == 422

    def test_create_alert_invalid_condition(self, client: TestClient) -> None:
        body = {"ticker": "AAPL", "condition": "sideways", "threshold": 100.0}
        resp = client.post("/api/alerts", json=body)
        assert resp.status_code == 422

    def test_delete_alert(self, client: TestClient) -> None:
        body = {"ticker": "TSLA", "condition": "below", "threshold": 150.0}
        created = client.post("/api/alerts", json=body).json()["alert"]
        alert_id = created["id"]

        resp = client.delete(f"/api/alerts/{alert_id}")
        assert resp.status_code == 200
        assert resp.json() == {"removed": alert_id}
        assert client.get("/api/alerts").json() == {"alerts": []}

    def test_delete_unknown_alert_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/api/alerts/does-not-exist")
        assert resp.status_code == 404

    def test_change_pct_alert_round_trip(self, client: TestClient) -> None:
        body = {
            "ticker": "msft",
            "condition": AlertCondition.CHANGE_PCT.value,
            "threshold": 5.0,
            "reference_price": 100.0,
        }
        created = client.post("/api/alerts", json=body).json()["alert"]
        # Stored ticker should be normalized to upper-case (matches AlertStore)
        assert created["ticker"] == "MSFT"
        assert created["condition"] == "change_pct"


class TestTaskRoutes:
    def test_list_tasks_empty(self, client: TestClient) -> None:
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == {"tasks": []}

    def test_list_tasks_picks_up_external_writes(self, client: TestClient, tmp_path: Path) -> None:
        # Simulate `qracer serve` writing a task while the web process is up.
        from qracer.tasks import TaskActionType, TaskStore

        store = TaskStore(tmp_path / "tasks.json")
        store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")

        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["action_type"] == "analyze"
        assert tasks[0]["action_params"] == {"ticker": "AAPL"}
        assert tasks[0]["schedule_spec"] == "every 1h"

    def test_delete_task(self, client: TestClient, tmp_path: Path) -> None:
        from qracer.tasks import TaskActionType, TaskStore

        store = TaskStore(tmp_path / "tasks.json")
        task = store.create(TaskActionType.NEWS_SCAN, {}, "every 30m")

        resp = client.delete(f"/api/tasks/{task.id}")
        assert resp.status_code == 200
        assert resp.json() == {"removed": task.id}
        assert client.get("/api/tasks").json() == {"tasks": []}

    def test_delete_unknown_task_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/api/tasks/does-not-exist")
        assert resp.status_code == 404


class TestWatchlistRoutes:
    def test_get_watchlist_empty(self, client: TestClient) -> None:
        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        assert resp.json() == {"tickers": []}

    def test_add_and_remove_ticker(self, client: TestClient) -> None:
        resp = client.post("/api/watchlist", json={"ticker": "aapl"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert body["added"] is True
        assert body["tickers"] == ["AAPL"]

        # Adding the same ticker again is a no-op.
        resp = client.post("/api/watchlist", json={"ticker": "AAPL"})
        assert resp.status_code == 201
        assert resp.json()["added"] is False

        resp = client.delete("/api/watchlist/AAPL")
        assert resp.status_code == 200
        assert resp.json() == {"removed": "AAPL", "tickers": []}

    def test_remove_unknown_ticker_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/api/watchlist/NOPE")
        assert resp.status_code == 404


class TestPortfolioRoute:
    def test_portfolio_uses_loaded_config(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build an isolated config dir with a single-holding portfolio.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "portfolio.toml").write_text(
            'currency = "USD"\n[[holdings]]\nticker = "AAPL"\nshares = 10\navg_cost = 150.0\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("QRACER_CONFIG_DIR", str(config_dir))

        # load_config has a module-level mtime cache; force reload.
        from qracer.config.loader import load_config

        load_config(force_reload=True)

        resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        body = resp.json()
        assert body["currency"] == "USD"
        assert body["total_value"] == pytest.approx(1500.0)
        assert len(body["holdings"]) == 1
        holding = body["holdings"][0]
        assert holding["ticker"] == "AAPL"
        assert holding["shares"] == 10
        assert holding["weight_pct"] == pytest.approx(100.0)


class TestDashboardUI:
    def test_stores_and_registries_attached(self, tmp_path: Path) -> None:
        from qracer.agents_store import AgentStore
        from qracer.memory.fact_store import FactStore

        app = create_app(user_dir=tmp_path)
        assert isinstance(app.state.agent_store, AgentStore)
        assert isinstance(app.state.fact_store, FactStore)
        # Registries are attached (may be empty/None if no providers configured).
        assert hasattr(app.state, "data_registry")
        assert hasattr(app.state, "llm_registry")

    def test_api_still_works_with_ui_mounted(self, client: TestClient) -> None:
        # NiceGUI owns "/", but the JSON API must remain intact.
        assert client.get("/api/health").status_code == 200

    def test_dashboard_page_served(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Guard against any accidental network call from the catalog fetch.
        monkeypatch.setattr("qracer.web.ui.list_models", lambda *a, **k: [])
        app = create_app(user_dir=tmp_path)
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200


def test_create_app_uses_user_dir(tmp_path: Path) -> None:
    """Routes should read/write the directory passed to create_app."""
    app = create_app(user_dir=tmp_path)
    with TestClient(app) as client:
        client.post("/api/watchlist", json={"ticker": "GOOG"})

    saved = json.loads((tmp_path / "watchlist.json").read_text(encoding="utf-8"))
    assert saved == ["GOOG"]
