"""NiceGUI web dashboard — portfolio, watchlist, briefing, agents, alerts, tasks, theses.

Runs in the same process as the FastAPI app, so it reads domain objects and calls the
registries directly (no REST round-trip) — the same in-process pattern as the agent
config UI. Price-based sections auto-refresh every few seconds via ``ui.timer``,
mirroring the Textual dashboard's ``set_interval``. v1 is read-only; the Agents tab is
the one editable section (reused from ``qracer.web.ui``).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from qracer.config.loader import load_config
from qracer.data.providers import PriceProvider

if TYPE_CHECKING:
    from fastapi import FastAPI

    from qracer.data.registry import DataRegistry
    from qracer.llm.registry import LLMRegistry
    from qracer.memory.fact_store import FactStore

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 5.0


def _catalyst_label(catalyst: str | None, catalyst_date: str | None) -> str:
    when = f" ({catalyst_date})" if catalyst_date else ""
    return f"{catalyst or '—'}{when}"


async def _fetch_prices(
    data_registry: "DataRegistry | None", tickers: list[str]
) -> dict[str, float]:
    """Best-effort concurrent price fetch; skips tickers that fail."""
    if data_registry is None or not tickers:
        return {}

    async def one(ticker: str) -> tuple[str, float | None]:
        try:
            price = await data_registry.async_get_with_fallback(PriceProvider, "get_price", ticker)
            return ticker, float(price)
        except Exception:  # noqa: BLE001 - best-effort per ticker
            return ticker, None

    results = await asyncio.gather(*(one(t) for t in tickers))
    return {t: p for t, p in results if p is not None}


def mount(
    app: "FastAPI",
    base_dir: Path,
    data_registry: "DataRegistry | None" = None,
    llm_registry: "LLMRegistry | None" = None,
    fact_store: "FactStore | None" = None,
) -> None:
    """Register the NiceGUI dashboard page at "/" and attach it to *app*."""
    from nicegui import ui

    from qracer.alerts import AlertStore
    from qracer.tasks import TaskStore
    from qracer.watchlist import Watchlist
    from qracer.web.ui import render_agents_section

    watchlist = Watchlist(base_dir / "watchlist.json")
    alert_store = AlertStore(base_dir / "alerts.json")
    task_store = TaskStore(base_dir / "tasks.json")

    @ui.page("/")
    async def index() -> None:
        prices: dict[str, float] = {}
        briefing: dict[str, object] = {"text": None, "loading": False}

        ui.label("qracer — Dashboard").classes("text-2xl font-bold q-pa-sm")

        # -- section builders (each reads the shared price cache / stores) --------

        def _portfolio_rows() -> tuple[list[dict], float]:
            cfg = load_config()
            holdings = cfg.portfolio.holdings
            if not holdings:
                return [], 0.0
            from qracer.risk.calculator import RiskCalculator

            price_map = {h.ticker: prices.get(h.ticker, h.avg_cost) for h in holdings}
            snap = RiskCalculator(cfg.portfolio).build_snapshot(price_map)
            rows = []
            for h in snap.holdings:
                pnl_sign = "+" if h.unrealized_pnl >= 0 else "-"
                rows.append(
                    {
                        "ticker": h.ticker,
                        "shares": f"{h.shares:g}",
                        "price": f"${h.current_price:,.2f}",
                        "value": f"${h.market_value:,.0f}",
                        "weight": f"{h.weight_pct:.1f}%",
                        "pnl": f"{pnl_sign}{abs(h.unrealized_pnl_pct):.1f}%",
                    }
                )
            return rows, snap.total_value

        @ui.refreshable
        def overview() -> None:
            rows, total = _portfolio_rows()
            with ui.card().classes("w-full"):
                ui.label("Portfolio").classes("font-bold")
                if rows:
                    ui.label(f"Total value: ${total:,.0f}")
                    for r in rows:
                        ui.label(f"{r['ticker']}  {r['price']}  {r['weight']}  P&L {r['pnl']}")
                else:
                    ui.label("No holdings configured.").classes("text-gray-500")

            with ui.card().classes("w-full"):
                ui.label("Watchlist").classes("font-bold")
                if watchlist.tickers:
                    for t in watchlist.tickers:
                        p = prices.get(t)
                        ui.label(f"{t}: {'$' + format(p, ',.2f') if p is not None else '—'}")
                else:
                    ui.label("Watchlist empty.").classes("text-gray-500")

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-3"):
                    ui.label("Today's briefing").classes("font-bold")
                    ui.button("Refresh", on_click=lambda: _load_briefing()).props("flat dense")
                if briefing["loading"]:
                    ui.label("Composing…").classes("text-gray-500")
                elif briefing["text"]:
                    ui.markdown(str(briefing["text"]))
                else:
                    ui.label(
                        "No briefing yet (needs holdings/watchlist and an LLM provider)."
                    ).classes("text-gray-500")

        @ui.refreshable
        def portfolio_section() -> None:
            rows, total = _portfolio_rows()
            if not rows:
                ui.label("No holdings. Add them to portfolio.toml.").classes("text-gray-500")
                return
            ui.table(
                columns=[
                    {"name": "ticker", "label": "Ticker", "field": "ticker"},
                    {"name": "shares", "label": "Shares", "field": "shares"},
                    {"name": "price", "label": "Price", "field": "price"},
                    {"name": "value", "label": "Value", "field": "value"},
                    {"name": "weight", "label": "Weight", "field": "weight"},
                    {"name": "pnl", "label": "P&L %", "field": "pnl"},
                ],
                rows=rows,
                row_key="ticker",
            ).classes("w-full")
            ui.label(f"Total value: ${total:,.0f}")

        @ui.refreshable
        def watchlist_section() -> None:
            if not watchlist.tickers:
                ui.label("Watchlist empty. Use 'qracer repl' → 'watch TICKER'.").classes(
                    "text-gray-500"
                )
                return
            rows = [
                {"ticker": t, "price": f"${prices[t]:,.2f}" if t in prices else "—"}
                for t in watchlist.tickers
            ]
            ui.table(
                columns=[
                    {"name": "ticker", "label": "Ticker", "field": "ticker"},
                    {"name": "price", "label": "Price", "field": "price"},
                ],
                rows=rows,
                row_key="ticker",
            ).classes("w-full")

        @ui.refreshable
        def alerts_section() -> None:
            alerts = alert_store.alerts
            if not alerts:
                ui.label("No alerts. Use 'qracer repl' → 'alert ...'.").classes("text-gray-500")
                return
            rows = [
                {
                    "ticker": a.ticker,
                    "condition": a.condition.value,
                    "threshold": f"{a.threshold:g}",
                    "status": "active" if a.active else "triggered",
                }
                for a in alerts
            ]
            ui.table(
                columns=[
                    {"name": "ticker", "label": "Ticker", "field": "ticker"},
                    {"name": "condition", "label": "Condition", "field": "condition"},
                    {"name": "threshold", "label": "Threshold", "field": "threshold"},
                    {"name": "status", "label": "Status", "field": "status"},
                ],
                rows=rows,
                row_key="ticker",
            ).classes("w-full")

        @ui.refreshable
        def tasks_section() -> None:
            tasks = task_store.get_active()
            if not tasks:
                ui.label("No scheduled tasks. Use 'qracer repl' → 'schedule ...'.").classes(
                    "text-gray-500"
                )
                return
            rows = [
                {
                    "action": t.describe(),
                    "schedule": t.schedule_spec,
                    "status": t.status.value,
                    "next": t.next_run_at or "—",
                }
                for t in tasks
            ]
            ui.table(
                columns=[
                    {"name": "action", "label": "Action", "field": "action"},
                    {"name": "schedule", "label": "Schedule", "field": "schedule"},
                    {"name": "status", "label": "Status", "field": "status"},
                    {"name": "next", "label": "Next Run", "field": "next"},
                ],
                rows=rows,
                row_key="action",
            ).classes("w-full")

        @ui.refreshable
        def theses_section() -> None:
            if fact_store is None:
                ui.label("No fact store.").classes("text-gray-500")
                return
            theses = fact_store.get_open_theses()
            if not theses:
                ui.label("No open theses. Run an analysis in 'qracer repl'.").classes(
                    "text-gray-500"
                )
                return
            rows = [
                {
                    "ticker": t.ticker,
                    "dir": "LONG" if t.target_price >= t.entry_zone_high else "SHORT",
                    "conv": f"{t.conviction}/10",
                    "target": f"${t.target_price:,.2f}",
                    "stop": f"${t.stop_loss:,.2f}",
                    "catalyst": _catalyst_label(t.catalyst, t.catalyst_date),
                }
                for t in theses
            ]
            ui.table(
                columns=[
                    {"name": "ticker", "label": "Ticker", "field": "ticker"},
                    {"name": "dir", "label": "Dir", "field": "dir"},
                    {"name": "conv", "label": "Conviction", "field": "conv"},
                    {"name": "target", "label": "Target", "field": "target"},
                    {"name": "stop", "label": "Stop", "field": "stop"},
                    {"name": "catalyst", "label": "Catalyst", "field": "catalyst"},
                ],
                rows=rows,
                row_key="ticker",
            ).classes("w-full")

        # -- refresh loop --------------------------------------------------------

        async def refresh_all() -> None:
            cfg = load_config()
            tickers = sorted({h.ticker for h in cfg.portfolio.holdings} | set(watchlist.tickers))
            new_prices = await _fetch_prices(data_registry, tickers)
            if new_prices:
                prices.update(new_prices)
            for section in (
                overview,
                portfolio_section,
                watchlist_section,
                alerts_section,
                tasks_section,
                theses_section,
            ):
                section.refresh()

        async def _load_briefing() -> None:
            if data_registry is None or llm_registry is None:
                briefing["text"] = None
                overview.refresh()
                return
            briefing["loading"] = True
            overview.refresh()
            try:
                from qracer.briefing import BriefingComposer

                cfg = load_config()
                composer = BriefingComposer(
                    data_registry, llm_registry, cfg.portfolio, watchlist, fact_store
                )
                briefing["text"] = await composer.compose()
            except Exception:  # noqa: BLE001 - surface as "unavailable", don't crash the page
                logger.warning("Briefing compose failed", exc_info=True)
                briefing["text"] = None
            finally:
                briefing["loading"] = False
                overview.refresh()

        # -- layout --------------------------------------------------------------

        with ui.tabs().classes("w-full") as tabs:
            t_overview = ui.tab("Overview")
            t_portfolio = ui.tab("Portfolio")
            t_watchlist = ui.tab("Watchlist")
            t_alerts = ui.tab("Alerts")
            t_tasks = ui.tab("Tasks")
            t_agents = ui.tab("Agents")
            t_theses = ui.tab("Theses")

        with ui.tab_panels(tabs, value=t_overview).classes("w-full"):
            with ui.tab_panel(t_overview):
                overview()
            with ui.tab_panel(t_portfolio):
                portfolio_section()
            with ui.tab_panel(t_watchlist):
                watchlist_section()
            with ui.tab_panel(t_alerts):
                alerts_section()
            with ui.tab_panel(t_tasks):
                tasks_section()
            with ui.tab_panel(t_agents):
                render_agents_section(base_dir)
            with ui.tab_panel(t_theses):
                theses_section()

        await refresh_all()
        asyncio.create_task(_load_briefing())
        ui.timer(REFRESH_SECONDS, refresh_all)

    ui.run_with(app, storage_secret="qracer-dashboard")


__all__ = ["mount"]
