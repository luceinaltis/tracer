"""Pure view-model builders shared by the web (NiceGUI) and TUI (Textual) dashboards.

Both dashboards showed the same data — portfolio holdings with P&L, watchlist prices,
alerts, tasks, open theses — and each formatted it independently. These functions are
the single source of that formatting: they take domain objects + a price map and return
plain dicts of display strings, with no UI dependency, so either front-end can render them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qracer.alerts import Alert
    from qracer.config.models import PortfolioConfig
    from qracer.memory.fact_store import PersistedThesis
    from qracer.tasks import Task


def catalyst_label(catalyst: str | None, catalyst_date: str | None) -> str:
    when = f" ({catalyst_date})" if catalyst_date else ""
    return f"{catalyst or '—'}{when}"


def portfolio_rows(
    portfolio: "PortfolioConfig", prices: dict[str, float]
) -> tuple[list[dict], float]:
    """Return (holding rows, total value). Missing prices fall back to avg cost."""
    holdings = portfolio.holdings
    if not holdings:
        return [], 0.0
    from qracer.risk.calculator import RiskCalculator

    price_map = {h.ticker: prices.get(h.ticker, h.avg_cost) for h in holdings}
    snap = RiskCalculator(portfolio).build_snapshot(price_map)
    rows: list[dict] = []
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


def watchlist_rows(tickers: list[str], prices: dict[str, float]) -> list[dict]:
    return [{"ticker": t, "price": f"${prices[t]:,.2f}" if t in prices else "—"} for t in tickers]


def alert_rows(alerts: "list[Alert]") -> list[dict]:
    return [
        {
            "ticker": a.ticker,
            "condition": a.condition.value,
            "threshold": f"{a.threshold:g}",
            "status": "active" if a.active else "triggered",
        }
        for a in alerts
    ]


def task_rows(tasks: "list[Task]") -> list[dict]:
    return [
        {
            "action": t.describe(),
            "schedule": t.schedule_spec,
            "status": t.status.value,
            "next": t.next_run_at or "—",
        }
        for t in tasks
    ]


def thesis_rows(theses: "list[PersistedThesis]") -> list[dict]:
    return [
        {
            "ticker": t.ticker,
            "dir": "LONG" if t.target_price >= t.entry_zone_high else "SHORT",
            "conv": f"{t.conviction}/10",
            "target": f"${t.target_price:,.2f}",
            "stop": f"${t.stop_loss:,.2f}",
            "catalyst": catalyst_label(t.catalyst, t.catalyst_date),
        }
        for t in theses
    ]


__all__ = [
    "alert_rows",
    "catalyst_label",
    "portfolio_rows",
    "task_rows",
    "thesis_rows",
    "watchlist_rows",
]
