"""Dashboard main-panel widgets for each sidebar section."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Label, Static

from qracer.config.loader import _user_dir, load_config
from qracer.data.prices import fetch_prices as _fetch_prices
from qracer.presenters import watchlist_rows
from qracer.watchlist import Watchlist

if TYPE_CHECKING:
    from qracer.alerts import AlertStore
    from qracer.data.registry import DataRegistry
    from qracer.tasks import TaskStore

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 5.0


def _watchlist_path() -> Path:
    return _user_dir() / "watchlist.json"


def _format_change(change: float) -> str:
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.2f}%"


# Price fetching is the shared best-effort helper (qracer.data.prices.fetch_prices),
# imported above as _fetch_prices so the call sites below are unchanged.


# ---------------------------------------------------------------------------
# DASH panels
# ---------------------------------------------------------------------------


class OverviewPanel(VerticalScroll):
    """Portfolio summary + market overview with live prices."""

    DEFAULT_CSS = """
    OverviewPanel {
        padding: 1 2;
    }
    .panel-title {
        text-style: bold;
        padding-bottom: 1;
    }
    .card {
        border: solid $primary-background;
        padding: 1 2;
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        *,
        data_registry: DataRegistry | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._data_registry = data_registry
        self._prices: dict[str, float] = {}

    def compose(self) -> ComposeResult:
        yield Label("Overview", classes="panel-title")

        with Vertical(classes="card"):
            yield Label("Portfolio Summary", classes="panel-title")
            yield DataTable(id="overview-portfolio-table")

        with Vertical(classes="card"):
            yield Label("Watchlist", classes="panel-title")
            yield DataTable(id="overview-watchlist-table")

    def on_mount(self) -> None:
        ptable = self.query_one("#overview-portfolio-table", DataTable)
        ptable.add_columns("Ticker", "Shares", "Avg Cost", "Price", "Value", "P&L %")
        self._populate_portfolio(ptable)

        wtable = self.query_one("#overview-watchlist-table", DataTable)
        wtable.add_columns("Ticker", "Price")
        self._populate_watchlist(wtable)

        self.set_interval(REFRESH_INTERVAL_SECONDS, self.refresh_data)

    def _populate_portfolio(self, table: DataTable) -> None:
        table.clear()
        cfg = load_config()
        if not cfg.portfolio.holdings:
            table.add_row("—", "—", "—", "—", "—", "—")
            return
        for h in cfg.portfolio.holdings:
            price = self._prices.get(h.ticker)
            if price is None:
                price_str = "—"
                value_str = f"${h.shares * h.avg_cost:,.2f}"
                pnl_str = "—"
            else:
                market_value = h.shares * price
                cost_basis = h.shares * h.avg_cost
                pnl_pct = (
                    ((market_value - cost_basis) / cost_basis * 100.0) if cost_basis > 0 else 0.0
                )
                price_str = f"${price:,.2f}"
                value_str = f"${market_value:,.2f}"
                pnl_str = _format_change(pnl_pct)
            table.add_row(
                h.ticker,
                f"{h.shares:.2f}",
                f"${h.avg_cost:,.2f}",
                price_str,
                value_str,
                pnl_str,
            )

    def _populate_watchlist(self, table: DataTable) -> None:
        table.clear()
        wl = Watchlist(_watchlist_path())
        if not wl.tickers:
            table.add_row("—", "—")
            return
        for row in watchlist_rows(list(wl.tickers), self._prices):
            table.add_row(row["ticker"], row["price"])

    async def refresh_data(self) -> None:
        """Fetch live prices and rebuild the tables."""
        cfg = load_config()
        wl = Watchlist(_watchlist_path())
        tickers = sorted({h.ticker for h in cfg.portfolio.holdings} | set(wl.tickers))

        new_prices = await _fetch_prices(self._data_registry, tickers)
        if new_prices:
            self._prices.update(new_prices)

        self._populate_portfolio(self.query_one("#overview-portfolio-table", DataTable))
        self._populate_watchlist(self.query_one("#overview-watchlist-table", DataTable))


class PortfolioPanel(VerticalScroll):
    """Holdings detail with P&L, allocation weight, and live market values."""

    DEFAULT_CSS = """
    PortfolioPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def __init__(
        self,
        *,
        data_registry: DataRegistry | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._data_registry = data_registry
        self._prices: dict[str, float] = {}

    def compose(self) -> ComposeResult:
        yield Label("Portfolio", classes="panel-title")
        yield Label("", id="portfolio-total")
        yield DataTable(id="portfolio-table")

    def on_mount(self) -> None:
        table = self.query_one("#portfolio-table", DataTable)
        table.add_columns("Ticker", "Shares", "Avg Cost", "Price", "Value", "Weight", "P&L %")
        self._populate(table)
        self.set_interval(REFRESH_INTERVAL_SECONDS, self.refresh_data)

    def _populate(self, table: DataTable) -> None:
        table.clear()
        cfg = load_config()
        holdings = cfg.portfolio.holdings
        total_label = self.query_one("#portfolio-total", Label)
        if not holdings:
            total_label.update("")
            table.add_row("—", "—", "—", "—", "—", "—", "—")
            return

        # Use RiskCalculator when we have a full set of live prices; otherwise
        # fall back to a cost-basis view so the user still sees their holdings.
        if self._prices and all(h.ticker in self._prices for h in holdings):
            from qracer.risk.calculator import RiskCalculator

            snapshot = RiskCalculator(cfg.portfolio).build_snapshot(self._prices)
            total_label.update(f"Total value: ${snapshot.total_value:,.2f} {snapshot.currency}")
            for hs in snapshot.holdings:
                table.add_row(
                    hs.ticker,
                    f"{hs.shares:.2f}",
                    f"${hs.avg_cost:,.2f}",
                    f"${hs.current_price:,.2f}",
                    f"${hs.market_value:,.2f}",
                    f"{hs.weight_pct:.1f}%",
                    _format_change(hs.unrealized_pnl_pct),
                )
            return

        # Fallback: show cost-basis view with "—" for missing prices.
        total_cost = sum(h.shares * h.avg_cost for h in holdings)
        total_label.update(f"Cost basis: ${total_cost:,.2f} {cfg.portfolio.currency}")
        for h in holdings:
            cost_value = h.shares * h.avg_cost
            weight = (cost_value / total_cost * 100) if total_cost > 0 else 0.0
            table.add_row(
                h.ticker,
                f"{h.shares:.2f}",
                f"${h.avg_cost:,.2f}",
                "—",
                f"${cost_value:,.2f}",
                f"{weight:.1f}%",
                "—",
            )

    async def refresh_data(self) -> None:
        """Fetch live prices for holdings and rebuild the table."""
        cfg = load_config()
        tickers = [h.ticker for h in cfg.portfolio.holdings]
        new_prices = await _fetch_prices(self._data_registry, tickers)
        if new_prices:
            self._prices.update(new_prices)
        self._populate(self.query_one("#portfolio-table", DataTable))


class WatchlistPanel(VerticalScroll):
    """Watchlist with live ticker prices."""

    DEFAULT_CSS = """
    WatchlistPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def __init__(
        self,
        *,
        data_registry: DataRegistry | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._data_registry = data_registry
        self._prices: dict[str, float] = {}

    def compose(self) -> ComposeResult:
        yield Label("Watchlist", classes="panel-title")
        yield DataTable(id="watchlist-table")
        yield Label("", id="watchlist-hint")

    def on_mount(self) -> None:
        table = self.query_one("#watchlist-table", DataTable)
        table.add_columns("Ticker", "Price")
        self._populate(table)
        self.set_interval(REFRESH_INTERVAL_SECONDS, self.refresh_data)

    def _populate(self, table: DataTable) -> None:
        table.clear()
        wl = Watchlist(_watchlist_path())
        hint = self.query_one("#watchlist-hint", Label)
        if not wl.tickers:
            hint.update("Watchlist is empty. Use 'qracer repl' → 'watch TICKER' to add.")
            return
        hint.update("")
        for row in watchlist_rows(list(wl.tickers), self._prices):
            table.add_row(row["ticker"], row["price"])

    async def refresh_data(self) -> None:
        """Fetch live prices for watchlist tickers and rebuild the table."""
        wl = Watchlist(_watchlist_path())
        new_prices = await _fetch_prices(self._data_registry, list(wl.tickers))
        if new_prices:
            self._prices.update(new_prices)
        self._populate(self.query_one("#watchlist-table", DataTable))


def _default_alerts_path() -> Path:
    return _user_dir() / "alerts.json"


def _default_tasks_path() -> Path:
    return _user_dir() / "tasks.json"


class AlertsPanel(VerticalScroll):
    """Active price alerts loaded from :class:`AlertStore` with hot-reload."""

    DEFAULT_CSS = """
    AlertsPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def __init__(
        self,
        *,
        alert_store: AlertStore | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._alert_store = alert_store

    def compose(self) -> ComposeResult:
        yield Label("Alerts", classes="panel-title")
        yield Label("", id="alerts-hint")
        yield DataTable(id="alerts-table")

    def on_mount(self) -> None:
        table = self.query_one("#alerts-table", DataTable)
        table.add_columns("Ticker", "Condition", "Threshold", "Status", "Created")
        self._populate(table)
        self.set_interval(REFRESH_INTERVAL_SECONDS, self.refresh_data)

    def _get_store(self) -> AlertStore | None:
        if self._alert_store is not None:
            return self._alert_store
        path = _default_alerts_path()
        if not path.exists():
            return None
        from qracer.alerts import AlertStore as _AlertStore

        self._alert_store = _AlertStore(path)
        return self._alert_store

    def _populate(self, table: DataTable) -> None:
        table.clear()
        hint = self.query_one("#alerts-hint", Label)
        store = self._get_store()
        if store is None:
            hint.update("No alerts. Use 'qracer repl' → 'alert ...' to create one.")
            return

        alerts = store.alerts  # triggers hot-reload via mtime check
        if not alerts:
            hint.update("No alerts. Use 'qracer repl' → 'alert ...' to create one.")
            return

        hint.update("")
        for alert in alerts:
            status = "active" if alert.active else "triggered"
            created = alert.created_at[:10] if alert.created_at else "—"
            table.add_row(
                alert.ticker,
                alert.condition.value,
                f"{alert.threshold:g}",
                status,
                created,
            )

    def refresh_data(self) -> None:
        """Reload alerts (hot-reloaded via :class:`AlertStore` mtime)."""
        self._populate(self.query_one("#alerts-table", DataTable))


class TasksPanel(VerticalScroll):
    """Scheduled tasks from :class:`TaskStore` with next run times."""

    DEFAULT_CSS = """
    TasksPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def __init__(
        self,
        *,
        task_store: TaskStore | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._task_store = task_store

    def compose(self) -> ComposeResult:
        yield Label("Tasks", classes="panel-title")
        yield Label("", id="tasks-hint")
        yield DataTable(id="tasks-table")

    def on_mount(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        table.add_columns("Action", "Schedule", "Status", "Next Run", "Runs")
        self._populate(table)
        self.set_interval(REFRESH_INTERVAL_SECONDS, self.refresh_data)

    def _get_store(self) -> TaskStore | None:
        if self._task_store is not None:
            return self._task_store
        path = _default_tasks_path()
        if not path.exists():
            return None
        from qracer.tasks import TaskStore as _TaskStore

        self._task_store = _TaskStore(path)
        return self._task_store

    def _populate(self, table: DataTable) -> None:
        table.clear()
        hint = self.query_one("#tasks-hint", Label)
        store = self._get_store()
        if store is None:
            hint.update("No scheduled tasks. Use 'qracer repl' → 'schedule ...' to create one.")
            return

        tasks = store.get_active()
        if not tasks:
            hint.update("No scheduled tasks. Use 'qracer repl' → 'schedule ...' to create one.")
            return

        hint.update("")
        for task in tasks:
            next_run = _format_next_run(task.next_run_at)
            table.add_row(
                task.describe(),
                task.schedule_spec,
                task.status.value,
                next_run,
                str(task.run_count),
            )

    def refresh_data(self) -> None:
        """Reload tasks (hot-reloaded via :class:`TaskStore` mtime)."""
        self._populate(self.query_one("#tasks-table", DataTable))


def _format_next_run(next_run_at: str | None) -> str:
    """Render an ISO timestamp as ``"in 5m"`` / ``"in 2h"`` / ``"due"``."""
    if not next_run_at:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(next_run_at)
    except ValueError:
        return next_run_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    delta = dt - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "due"
    if total_seconds < 60:
        return f"in {total_seconds}s"
    if total_seconds < 3600:
        return f"in {total_seconds // 60}m"
    if total_seconds < 86400:
        return f"in {total_seconds // 3600}h"
    return f"in {total_seconds // 86400}d"


# ---------------------------------------------------------------------------
# CHAT panels
# ---------------------------------------------------------------------------


class NewChatPanel(VerticalScroll):
    """Placeholder for starting a new chat session."""

    DEFAULT_CSS = """
    NewChatPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("New Chat", classes="panel-title")
        yield Static(
            "Start a new conversation session.\n\n"
            "Use 'qracer repl' for the full conversational agent experience.\n"
            "Dashboard chat integration is planned for a future release."
        )


class HistoryPanel(VerticalScroll):
    """Show past conversation sessions."""

    DEFAULT_CSS = """
    HistoryPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("Chat History", classes="panel-title")
        yield DataTable(id="history-table")

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.add_columns("Session ID", "Date", "Queries")
        self._populate(table)

    def _populate(self, table: DataTable) -> None:
        table.clear()
        sessions_dir = _user_dir() / "sessions"
        if not sessions_dir.is_dir():
            return
        session_files = sorted(sessions_dir.glob("*.jsonl"), reverse=True)[:20]
        if not session_files:
            return
        for f in session_files:
            sid = f.stem
            mtime = f.stat().st_mtime
            dt = datetime.datetime.fromtimestamp(mtime)
            line_count = sum(1 for _ in f.open(encoding="utf-8"))
            table.add_row(sid, dt.strftime("%Y-%m-%d %H:%M"), str(line_count))


# ---------------------------------------------------------------------------
# SETTINGS panels
# ---------------------------------------------------------------------------


class GeneralSettingsPanel(VerticalScroll):
    """Display general application settings."""

    DEFAULT_CSS = """
    GeneralSettingsPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    .setting-row { padding: 0 0 0 1; height: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("General Settings", classes="panel-title")
        yield DataTable(id="general-settings-table")

    def on_mount(self) -> None:
        table = self.query_one("#general-settings-table", DataTable)
        table.add_columns("Setting", "Value")
        cfg = load_config()
        table.add_row("Language", cfg.app.language)
        table.add_row("Default Mode", cfg.app.default_mode)
        table.add_row("LLM Provider", cfg.app.llm_provider)
        table.add_row("LLM Model", cfg.app.llm_model)
        table.add_row("Currency", cfg.portfolio.currency)


class ProvidersSettingsPanel(VerticalScroll):
    """Display provider configuration."""

    DEFAULT_CSS = """
    ProvidersSettingsPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("Providers", classes="panel-title")
        yield DataTable(id="providers-table")

    def on_mount(self) -> None:
        table = self.query_one("#providers-table", DataTable)
        table.add_columns("Provider", "Kind", "Enabled", "Tier", "Priority", "API Key Env")
        cfg = load_config()
        for name, prov in cfg.providers.providers.items():
            table.add_row(
                name,
                prov.kind,
                "✓" if prov.enabled else "✗",
                prov.tier,
                str(prov.priority),
                prov.api_key_env or "—",
            )


class NotificationsSettingsPanel(VerticalScroll):
    """Notification channel settings placeholder."""

    DEFAULT_CSS = """
    NotificationsSettingsPanel { padding: 1 2; }
    .panel-title { text-style: bold; padding-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("Notifications", classes="panel-title")
        yield Static(
            "Notification channels are not yet configured.\n\n"
            "Telegram integration is planned (see issue #38).\n"
            "Price alert thresholds are planned (see issue #45)."
        )
