"""Unit tests for the dashboard's pure row-builders.

The NiceGUI page in ``qracer.web.dashboard`` delegates all data shaping to small
pure functions (``_portfolio_rows``, ``_watchlist_rows``, ``_alert_rows``,
``_task_rows``, ``_thesis_rows``). Those are tested here directly, without
rendering any NiceGUI widgets. (Price fetching moved to ``tests/data/test_prices.py``.)
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("nicegui")

from qracer.alerts import Alert, AlertCondition  # noqa: E402
from qracer.config.models import Holding, PortfolioConfig  # noqa: E402
from qracer.memory.fact_models import PersistedThesis, ThesisStatus  # noqa: E402
from qracer.web import dashboard  # noqa: E402


class TestCatalystLabel:
    def test_with_date(self) -> None:
        assert dashboard._catalyst_label("Earnings", "2026-05-01") == "Earnings (2026-05-01)"

    def test_without_date(self) -> None:
        assert dashboard._catalyst_label("Earnings", None) == "Earnings"

    def test_no_catalyst(self) -> None:
        assert dashboard._catalyst_label(None, None) == "—"


class TestPortfolioRows:
    def test_empty_portfolio(self) -> None:
        rows, total = dashboard._portfolio_rows(PortfolioConfig(holdings=[]), {})
        assert rows == []
        assert total == 0.0

    def test_uses_live_price_and_formats(self) -> None:
        portfolio = PortfolioConfig(holdings=[Holding(ticker="AAPL", shares=10, avg_cost=100.0)])
        rows, total = dashboard._portfolio_rows(portfolio, {"AAPL": 150.0})
        assert len(rows) == 1
        assert rows[0]["ticker"] == "AAPL"
        assert rows[0]["price"] == "$150.00"
        assert rows[0]["pnl"].startswith("+")  # 150 > 100 avg cost
        assert total == pytest.approx(1500.0)

    def test_falls_back_to_avg_cost_when_price_missing(self) -> None:
        portfolio = PortfolioConfig(holdings=[Holding(ticker="AAPL", shares=10, avg_cost=100.0)])
        rows, _ = dashboard._portfolio_rows(portfolio, {})  # no live price
        assert rows[0]["price"] == "$100.00"
        assert rows[0]["pnl"] == "+0.0%"


class TestWatchlistRows:
    def test_priced_and_unpriced(self) -> None:
        rows = dashboard._watchlist_rows(["AAPL", "MSFT"], {"AAPL": 150.0})
        assert rows == [
            {"ticker": "AAPL", "price": "$150.00"},
            {"ticker": "MSFT", "price": "—"},
        ]


class TestAlertRows:
    def test_active_and_triggered(self) -> None:
        alerts = [
            Alert(
                id="1",
                ticker="AAPL",
                condition=AlertCondition.ABOVE,
                threshold=200.0,
                created_at="2026-01-01",
                active=True,
            ),
            Alert(
                id="2",
                ticker="MSFT",
                condition=AlertCondition.BELOW,
                threshold=300.0,
                created_at="2026-01-01",
                active=False,
            ),
        ]
        rows = dashboard._alert_rows(alerts)
        assert rows[0]["status"] == "active"
        assert rows[0]["threshold"] == "200"
        assert rows[1]["status"] == "triggered"


class TestTaskRows:
    def test_maps_fields(self) -> None:
        task = SimpleNamespace(
            describe=lambda: "news AAPL",
            schedule_spec="0 9 * * *",
            status=SimpleNamespace(value="active"),
            next_run_at="2026-05-01T09:00",
        )
        rows = dashboard._task_rows([task])  # type: ignore[list-item]
        assert rows == [
            {
                "action": "news AAPL",
                "schedule": "0 9 * * *",
                "status": "active",
                "next": "2026-05-01T09:00",
            }
        ]

    def test_missing_next_run_dash(self) -> None:
        task = SimpleNamespace(
            describe=lambda: "x",
            schedule_spec="* * * * *",
            status=SimpleNamespace(value="paused"),
            next_run_at=None,
        )
        rows = dashboard._task_rows([task])  # type: ignore[list-item]
        assert rows[0]["next"] == "—"


class TestThesisRows:
    def _thesis(self, target: float, entry_high: float) -> PersistedThesis:
        now = datetime(2026, 1, 1)
        return PersistedThesis(
            id=1,
            ticker="AAPL",
            entry_zone_low=90.0,
            entry_zone_high=entry_high,
            target_price=target,
            stop_loss=80.0,
            risk_reward_ratio=2.0,
            catalyst="Earnings",
            catalyst_date="2026-05-01",
            conviction=7,
            summary="s",
            status=ThesisStatus.OPEN,
            session_id="sess",
            created_at=now,
            updated_at=now,
        )

    def test_long_when_target_above_entry(self) -> None:
        rows = dashboard._thesis_rows([self._thesis(target=200.0, entry_high=100.0)])
        assert rows[0]["dir"] == "LONG"
        assert rows[0]["conv"] == "7/10"
        assert rows[0]["catalyst"] == "Earnings (2026-05-01)"

    def test_short_when_target_below_entry(self) -> None:
        rows = dashboard._thesis_rows([self._thesis(target=50.0, entry_high=100.0)])
        assert rows[0]["dir"] == "SHORT"
