"""CLI entrypoint — Click-based commands for qracer."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from qracer.config.models import QracerConfig
    from qracer.data.providers import StreamingProvider
    from qracer.data.registry import DataRegistry
    from qracer.llm.registry import LLMRegistry

from qracer.config.loader import (
    _load_toml,
    _user_dir,
    load_config,
    resolve_config_dirs,
)

logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).parent / "config" / "schema"

BANNER = """\
╔══════════════════════════════════════════╗
║  qracer — conversational alpha engine   ║
╚══════════════════════════════════════════╝
Type your query, or 'quit' to exit.
Commands: save, save json, save pdf, backtest, help
"""


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """qracer — conversational investment agent CLI."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# qracer install
# ---------------------------------------------------------------------------


_LLM_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude (Anthropic)",
    "openai": "OpenAI (GPT-4o)",
    "gemini": "Gemini (Google)",
}


def _collect_llm_choices() -> list[tuple[str, str, str | None]]:
    """Return ``[(name, display_label, api_key_env), ...]`` for LLM providers."""
    schema_data = _load_toml(SCHEMA_DIR / "providers.toml")
    result: list[tuple[str, str, str | None]] = []
    for name, cfg in schema_data.get("providers", {}).items():
        if cfg.get("kind") == "llm":
            label = _LLM_DISPLAY_NAMES.get(name) or name.capitalize()
            env: str | None = cfg.get("api_key_env")
            result.append((name, label, env))
    return result


def _update_provider_selection(
    providers_path: Path,
    chosen: str,
    all_llm: list[tuple[str, str, str | None]],
) -> None:
    """Enable the chosen LLM provider and disable others in providers.toml."""
    if not providers_path.exists():
        return
    text = providers_path.read_text(encoding="utf-8")
    for name, _, _ in all_llm:
        enable = "true" if name == chosen else "false"
        pattern = rf"(\[providers\.{re.escape(name)}\][^\[]*?)enabled\s*=\s*(true|false)"
        text = re.sub(pattern, rf"\1enabled = {enable}", text)
    providers_path.write_text(text, encoding="utf-8")


@main.command()
def install() -> None:
    """Interactive first-time setup wizard."""
    home_dir = _user_dir()
    click.echo(f"Setting up qracer config in {home_dir}\n")

    # 1. Create directory
    home_dir.mkdir(parents=True, exist_ok=True)

    # 2. Copy default config files
    for name in ("config.toml", "providers.toml", "portfolio.toml"):
        src = SCHEMA_DIR / name
        dest = home_dir / name
        if dest.exists():
            click.echo(f"  {name} already exists, skipping.")
        else:
            shutil.copy2(src, dest)
            click.echo(f"  Created {name}")

    # 3. LLM provider selection
    llm_choices = _collect_llm_choices()
    click.echo("\n  Select LLM provider:")
    for i, (_, label, _) in enumerate(llm_choices, 1):
        click.echo(f"    {i}. {label}")

    choice_idx = click.prompt("  Choice", type=click.IntRange(1, len(llm_choices)), default=1)
    chosen_name, chosen_label, chosen_env = llm_choices[choice_idx - 1]

    # 4. Prompt for the chosen provider's API key only
    creds: dict[str, str] = {}
    if chosen_env:
        value = click.prompt(f"\n  {chosen_env}", default="", show_default=False).strip()
        if value:
            creds[chosen_env] = value

    # 5. Prompt for portfolio currency
    currency = click.prompt("\n  Portfolio currency", default="USD").strip()

    # 6. Write credentials.env
    creds_path = home_dir / "credentials.env"
    lines = [f"{k}={v}" for k, v in creds.items()]
    creds_path.write_text("\n".join(lines) + "\n" if lines else "")

    # 7. Enable chosen LLM provider in providers.toml
    _update_provider_selection(home_dir / "providers.toml", chosen_name, llm_choices)

    # 8. Update portfolio currency if non-default
    if currency != "USD":
        portfolio_path = home_dir / "portfolio.toml"
        if portfolio_path.exists():
            text = portfolio_path.read_text(encoding="utf-8")
            text = text.replace('currency = "USD"', f'currency = "{currency}"')
            portfolio_path.write_text(text, encoding="utf-8")

    click.echo(f"\n✓ Setup complete! {chosen_label} enabled as LLM provider.")

    # Warn if API key was not provided
    if chosen_env and not creds.get(chosen_env) and not os.environ.get(chosen_env):
        click.echo(f"\n⚠ {chosen_env} not set — set it before running 'qracer repl'.")

    click.echo("\nNext steps:")
    click.echo("  qracer status   — check configuration")
    click.echo("  qracer repl     — start interactive session")


# ---------------------------------------------------------------------------
# qracer status
# ---------------------------------------------------------------------------


@main.command()
def status() -> None:
    """Show system status."""
    dirs = resolve_config_dirs()

    # Config directories
    if dirs:
        click.echo("Config directories:")
        for d in dirs:
            click.echo(f"  ✓ {d}")
    else:
        click.echo("No config directory found. Run 'qracer install' first.")
        return

    cfg = load_config(force_reload=True)

    # Data providers
    click.echo("\nData providers:")
    data_providers = {n: p for n, p in cfg.providers.providers.items() if p.kind == "data"}
    if data_providers:
        for name, prov in data_providers.items():
            state = "enabled" if prov.enabled else "disabled"
            click.echo(f"  {name}: {state} (tier={prov.tier}, priority={prov.priority})")
    else:
        click.echo("  (none configured)")

    # LLM providers
    click.echo("\nLLM providers:")
    llm_providers = {n: p for n, p in cfg.providers.providers.items() if p.kind == "llm"}
    if llm_providers:
        for name, prov in llm_providers.items():
            env_key = prov.api_key_env
            has_key = bool(env_key and (cfg.credentials.get(env_key) or os.environ.get(env_key)))
            if not prov.enabled:
                click.echo(f"  {name}: disabled")
            elif has_key:
                click.echo(f"  ✓ {name}: ready")
            else:
                click.echo(f"  ✗ {name}: {env_key} not set")
    else:
        click.echo("  (none configured)")

    # Portfolio
    click.echo(
        f"\nPortfolio: {len(cfg.portfolio.holdings)} holdings, currency={cfg.portfolio.currency}"
    )

    # Credentials
    click.echo("\nCredentials:")
    if cfg.credentials:
        for key, val in cfg.credentials.items():
            masked = val[:2] + "***" + val[-2:] if len(val) > 4 else "***"
            click.echo(f"  {key}: {masked}")
    else:
        click.echo("  (none set)")


# ---------------------------------------------------------------------------
# qracer config
# ---------------------------------------------------------------------------


@main.command("config")
@click.option("--set", "set_value", default=None, help="Set a value: key=value")
def config_cmd(set_value: str | None) -> None:
    """Show or update current merged config."""
    if set_value is not None:
        _config_set(set_value)
        return

    cfg = load_config(force_reload=True)
    click.echo(cfg.model_dump_json(indent=2))


def _config_set(pair: str) -> None:
    """Write key=value into ~/.qracer/config.toml."""
    if "=" not in pair:
        raise click.BadParameter("Expected key=value format", param_hint="--set")

    key, _, value = pair.partition("=")
    key = key.strip()
    value = value.strip()

    home_dir = _user_dir()
    config_path = home_dir / "config.toml"

    if not config_path.exists():
        click.echo("No config.toml found. Run 'qracer install' first.")
        raise SystemExit(1)

    # Load existing, update, and write back
    data = _load_toml(config_path)
    data[key] = value
    _write_toml(config_path, data)
    click.echo(f"Set {key}={value} in {config_path}")


def _write_toml(path: Path, data: dict[str, object]) -> None:
    """Write a flat dict back to TOML."""
    lines: list[str] = []
    for k, v in data.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        else:
            lines.append(f'{k} = "{v}"')
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# qracer repl
# ---------------------------------------------------------------------------


def _build_streaming_adapter(config: QracerConfig) -> StreamingProvider | None:
    """Return a Finnhub streaming adapter if enabled and available.

    The adapter is only constructed when the ``finnhub`` provider is
    enabled in ``providers.toml`` *and* an API key is available *and*
    the ``websockets`` package is importable.  Any failure returns
    ``None`` so the caller falls back to REST polling.
    """
    finnhub_cfg = config.providers.providers.get("finnhub")
    if finnhub_cfg is None or not finnhub_cfg.enabled:
        return None
    api_key_env = finnhub_cfg.api_key_env or "FINNHUB_API_KEY"
    api_key = config.credentials.get(api_key_env) or os.environ.get(api_key_env)
    if not api_key:
        return None
    try:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        return FinnhubWebSocketAdapter(api_key=api_key)
    except ImportError:
        logger.info("Streaming disabled — install 'qracer[streaming]' for WebSocket support")
        return None
    except Exception as exc:
        logger.warning("Streaming adapter unavailable: %s", exc)
        return None


def _build_registries() -> tuple[LLMRegistry, DataRegistry, list[str]]:
    """Build LLM and data registries from providers.toml + provider catalog.

    Returns ``(llm_registry, data_registry, warnings)`` where *warnings*
    is a list of human-readable strings describing providers that could
    not be loaded.
    """
    import importlib

    from qracer.data.registry import DataRegistry
    from qracer.llm.providers import Role
    from qracer.llm.registry import LLMRegistry
    from qracer.provider_catalog import discover_data_providers, discover_llm_providers

    config = load_config()
    llm_registry = LLMRegistry()
    data_registry = DataRegistry()
    warnings: list[str] = []

    # Discover providers: built-ins + any installed entry-point plugins.
    data_catalog = discover_data_providers()
    llm_catalog = discover_llm_providers()

    sorted_providers = sorted(
        config.providers.providers.items(),
        key=lambda item: item[1].priority,
    )

    for name, prov_cfg in sorted_providers:
        if not prov_cfg.enabled:
            continue

        # Resolve API key (shared by data and llm paths)
        api_key: str | None = None
        if prov_cfg.api_key_env:
            api_key = config.credentials.get(prov_cfg.api_key_env) or os.environ.get(
                prov_cfg.api_key_env
            )
            if not api_key:
                msg = f"{name}: {prov_cfg.api_key_env} not set — skipped"
                warnings.append(msg)
                logger.warning("Provider '%s' skipped: %s not set", name, prov_cfg.api_key_env)
                continue

        if prov_cfg.kind == "data" and name in data_catalog:
            adapter_path, cap_paths = data_catalog[name]
            try:
                mod_path, cls_name = adapter_path.rsplit(".", 1)
                adapter_cls = getattr(importlib.import_module(mod_path), cls_name)
                adapter = adapter_cls(api_key=api_key) if api_key else adapter_cls()
                caps = []
                for cp in cap_paths:
                    cp_mod, cp_name = cp.rsplit(".", 1)
                    caps.append(getattr(importlib.import_module(cp_mod), cp_name))
                data_registry.register(name, adapter, caps)
            except Exception as exc:
                msg = f"{name}: {exc}"
                warnings.append(msg)
                logger.warning("Data provider '%s' unavailable: %s", name, exc)

        elif prov_cfg.kind == "llm" and name in llm_catalog:
            adapter_path, role_values = llm_catalog[name]
            try:
                mod_path, cls_name = adapter_path.rsplit(".", 1)
                adapter_cls = getattr(importlib.import_module(mod_path), cls_name)
                adapter = adapter_cls(api_key=api_key)
                roles = [Role(v) for v in role_values]
                llm_registry.register(name, adapter, roles)
            except Exception as exc:
                msg = f"{name}: {exc}"
                warnings.append(msg)
                logger.warning("LLM provider '%s' unavailable: %s", name, exc)

    return llm_registry, data_registry, warnings


_HELP_TEXT = """\
Available commands:
  save              Save last analysis as Markdown
  save json         Save last analysis as JSON
  save pdf          Save last analysis as PDF (requires qracer[pdf] extra)
  backtest          Backtest the last trade thesis against historical data
  watchlist         Show watchlist with current prices
  watch TICKER      Add ticker to watchlist
  unwatch TICKER    Remove ticker from watchlist
  alert TICKER above/below PRICE  Set a price alert
  alert TICKER change PERCENT     Set a % change alert
  alerts            Show all alerts
  remove-alert ID   Remove an alert by ID
  help              Show this help
  quit              Exit

Tips:
  - Ask about any ticker: "Analyze AAPL", "Why did TSLA spike?"
  - Compare tickers: "Compare AAPL and MSFT"
  - Follow up naturally: "What about Samsung?", "More details?"
  - Set alerts: "alert AAPL above 200", "alert TSLA below 150"
  - Schedule tasks: "schedule analyze AAPL every 1h", "schedule news scan TSLA at 2026-04-08T09:30"
  - View tasks: "tasks"
  - Cancel a task: "cancel-task <id>"

Note: qracer provides research analysis only, not investment advice.
      It cannot execute trades or predict future prices.
"""


async def _repl_loop(
    engine: object,
    watchlist: object,
    alert_monitor: object | None = None,
    task_executor: object | None = None,
    data_registry: object | None = None,
    sessions_dir: Path | None = None,
    current_session: Path | None = None,
    fact_store: object | None = None,
) -> None:
    """Run the interactive read-eval-print loop."""
    from qracer.alert_monitor import AlertMonitor
    from qracer.config.loader import has_config_changed
    from qracer.conversation.quickpath import generate_briefing
    from qracer.data.registry import DataRegistry
    from qracer.task_executor import TaskExecutor
    from qracer.watchlist import Watchlist

    monitor: AlertMonitor | None = alert_monitor  # type: ignore[assignment]
    executor: TaskExecutor | None = task_executor  # type: ignore[assignment]

    # One-time session-start briefing summarising activity since the last run.
    if (
        sessions_dir is not None
        and isinstance(data_registry, DataRegistry)
        and isinstance(watchlist, Watchlist)
        and monitor is not None
        and executor is not None
    ):
        try:
            briefing = await generate_briefing(
                watchlist,
                data_registry,
                monitor.store,
                executor.store,
                sessions_dir,
                current_session=current_session,
                fact_store=fact_store,  # type: ignore[arg-type]
            )
        except Exception:
            logger.debug("Session briefing generation failed", exc_info=True)
            briefing = None
        if briefing:
            click.echo(briefing)
            click.echo()

    click.echo(BANNER)

    while True:
        # Check alerts on each iteration if enough time has elapsed.
        if monitor and monitor.should_check():
            try:
                triggered = await monitor.check()
                for result in triggered:
                    click.echo(f"🔔 {result.message}")
            except Exception:
                logger.debug("Alert check failed", exc_info=True)

        # Check scheduled tasks.
        if executor and executor.should_check():
            try:
                task_results = await executor.check()
                for tr in task_results:
                    status = "✓" if tr.success else "✗"
                    click.echo(f"📋 [{status}] {tr.task.describe()}: {tr.output or tr.error}")
            except Exception:
                logger.debug("Task check failed", exc_info=True)

        try:
            user_input = input("qracer> ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo("\nGoodbye.")
            break

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd in ("quit", "exit", "q"):
            click.echo("Goodbye.")
            break

        if cmd in ("help", "/help"):
            click.echo(_HELP_TEXT)
            continue

        # Hot-plug: reload config and rebuild registries if files changed
        if has_config_changed():
            try:
                llm_reg, data_reg, reload_warnings = _build_registries()

                # Re-read config for portfolio and pipeline updates.
                from qracer.tools.pipeline import configure as configure_pipeline

                reloaded = load_config(force_reload=True)
                configure_pipeline(
                    lookback_days=reloaded.app.lookback_days,
                    staleness_hours=reloaded.app.staleness_hours,
                )

                engine.update_registries(  # type: ignore[attr-defined]
                    llm_reg, data_reg, portfolio_config=reloaded.portfolio
                )
                click.echo("⟳ Configuration reloaded.")
                for warn in reload_warnings:
                    click.echo(f"  ⚠ {warn}")
                click.echo()
            except Exception:
                logger.warning("Config reload failed", exc_info=True)

        if cmd in ("save", "save analysis", "/save"):
            path = engine.save_last_report()  # type: ignore[attr-defined]
            if path:
                click.echo(f"Saved to {path}\n")
            else:
                click.echo("No analysis to save. Run a query first.\n")
            continue

        if cmd in ("save json", "/save json"):
            path = engine.save_last_report(fmt="json")  # type: ignore[attr-defined]
            if path:
                click.echo(f"Saved to {path}\n")
            else:
                click.echo("No analysis to save. Run a query first.\n")
            continue

        if cmd in ("save pdf", "/save pdf"):
            try:
                path = engine.save_last_report(fmt="pdf")  # type: ignore[attr-defined]
            except ImportError as exc:
                click.echo(f"{exc}\n")
                continue
            if path:
                click.echo(f"Saved to {path}\n")
            else:
                click.echo("No analysis to save. Run a query first.\n")
            continue

        # Watchlist commands
        if cmd in ("watchlist", "wl", "/watchlist"):
            _show_watchlist(watchlist)  # type: ignore[arg-type]
            continue

        if cmd.startswith(("watch ", "/watch ")):
            ticker = user_input.split(maxsplit=1)[1].strip().upper()
            if watchlist.add(ticker):  # type: ignore[attr-defined]
                click.echo(f"Added {ticker} to watchlist.\n")
            else:
                click.echo(f"{ticker} is already on your watchlist.\n")
            continue

        if cmd.startswith(("unwatch ", "/unwatch ")):
            ticker = user_input.split(maxsplit=1)[1].strip().upper()
            if watchlist.remove(ticker):  # type: ignore[attr-defined]
                click.echo(f"Removed {ticker} from watchlist.\n")
            else:
                click.echo(f"{ticker} is not on your watchlist.\n")
            continue

        # Alert commands
        if cmd in ("alerts", "/alerts"):
            _show_alerts(monitor)
            continue

        if cmd.startswith(("alert ", "/alert ")):
            _handle_alert_command(user_input, monitor)
            continue

        if cmd.startswith(("remove-alert ", "/remove-alert ")):
            _handle_remove_alert(user_input, monitor)
            continue

        # Task commands
        if cmd in ("tasks", "/tasks"):
            _show_tasks(executor)
            continue

        if cmd.startswith(("schedule ", "/schedule ")):
            _handle_schedule_command(user_input, executor)
            continue

        if cmd.startswith(("cancel-task ", "/cancel-task ")):
            _handle_cancel_task(user_input, executor)
            continue

        # Backtest command
        if cmd in ("backtest", "/backtest"):
            await _handle_backtest(engine, data_registry)
            continue

        # Show progress while query is processing.
        click.echo("Analyzing...", nl=False)
        try:
            response = await engine.query(user_input)  # type: ignore[attr-defined]
            click.echo("\r" + " " * 20 + "\r", nl=False)  # clear "Analyzing..."
            click.echo(response.text)
            click.echo()
        except KeyError as exc:
            click.echo("\r" + " " * 20 + "\r", nl=False)
            click.echo(f"Missing component: {exc}")
            click.echo("Hint: run 'qracer status' to check provider configuration.\n")
        except Exception as exc:
            click.echo("\r" + " " * 20 + "\r", nl=False)
            logger.exception("Error processing query")
            click.echo(f"Something went wrong: {type(exc).__name__}")
            click.echo("Hint: try rephrasing your query or check 'qracer status'.\n")


async def _handle_backtest(engine: object, data_registry: object | None) -> None:
    """Run a backtest on the last trade thesis."""
    from qracer.backtest import Backtester, format_backtest_result
    from qracer.conversation.engine import ConversationEngine

    eng: ConversationEngine = engine  # type: ignore[assignment]
    last = eng._last_response
    if last is None or last.analysis.trade_thesis is None:
        click.echo("No trade thesis to backtest. Run an analysis first.\n")
        return

    if data_registry is None:
        click.echo("Backtest unavailable (no data provider configured).\n")
        return

    thesis = last.analysis.trade_thesis
    backtester = Backtester(data_registry)  # type: ignore[arg-type]
    click.echo("Backtesting...", nl=False)
    try:
        result = await backtester.run(thesis)
        click.echo("\r" + " " * 20 + "\r", nl=False)
        click.echo(format_backtest_result(result, thesis))
        click.echo()
    except Exception as exc:
        click.echo("\r" + " " * 20 + "\r", nl=False)
        logger.exception("Backtest failed")
        click.echo(f"Backtest failed: {type(exc).__name__}: {exc}\n")


def _show_alerts(monitor: object | None) -> None:
    """Display all alerts."""
    from qracer.alert_monitor import AlertMonitor

    if monitor is None:
        click.echo("Alerts are not available (no data provider configured).\n")
        return

    mon: AlertMonitor = monitor  # type: ignore[assignment]
    all_alerts = mon.store.alerts
    if not all_alerts:
        click.echo("No alerts set. Use 'alert TICKER above/below PRICE' to create one.\n")
        return

    active = [a for a in all_alerts if a.active]
    triggered = [a for a in all_alerts if not a.active]

    if active:
        click.echo(f"Active alerts ({len(active)}):")
        for a in active:
            click.echo(f"  [{a.id}] {a.describe()}")

    if triggered:
        click.echo(f"Triggered alerts ({len(triggered)}):")
        for a in triggered:
            click.echo(f"  [{a.id}] {a.describe()} (triggered {a.triggered_at})")

    click.echo()


def _handle_alert_command(user_input: str, monitor: object | None) -> None:
    """Parse and create a price alert from user input.

    Supported formats:
        alert TICKER above PRICE
        alert TICKER below PRICE
        alert TICKER change PERCENT
    """
    from qracer.alert_monitor import AlertMonitor
    from qracer.alerts import AlertCondition

    if monitor is None:
        click.echo("Alerts are not available (no data provider configured).\n")
        return

    mon: AlertMonitor = monitor  # type: ignore[assignment]
    parts = user_input.split()
    # Expected: ["alert", TICKER, CONDITION, VALUE]
    if len(parts) < 4:
        click.echo("Usage: alert TICKER above/below PRICE  or  alert TICKER change PERCENT\n")
        return

    ticker = parts[1].upper()
    condition_str = parts[2].lower()
    try:
        value = float(parts[3])
    except ValueError:
        click.echo(f"Invalid number: {parts[3]}\n")
        return

    condition_map = {
        "above": AlertCondition.ABOVE,
        "below": AlertCondition.BELOW,
        "change": AlertCondition.CHANGE_PCT,
        "change_pct": AlertCondition.CHANGE_PCT,
    }
    condition = condition_map.get(condition_str)
    if condition is None:
        click.echo(f"Unknown condition: {condition_str}. Use above, below, or change.\n")
        return

    alert = mon.store.create(ticker, condition, value)
    click.echo(f"Alert set: {alert.describe()} [{alert.id}]\n")


def _handle_remove_alert(user_input: str, monitor: object | None) -> None:
    """Remove an alert by ID."""
    from qracer.alert_monitor import AlertMonitor

    if monitor is None:
        click.echo("Alerts are not available (no data provider configured).\n")
        return

    mon: AlertMonitor = monitor  # type: ignore[assignment]
    parts = user_input.split()
    if len(parts) < 2:
        click.echo("Usage: remove-alert ID\n")
        return

    alert_id = parts[1]
    if mon.store.remove(alert_id):
        click.echo(f"Alert {alert_id} removed.\n")
    else:
        click.echo(f"No alert found with ID {alert_id}.\n")


def _show_watchlist(watchlist: object) -> None:
    """Display the current watchlist."""
    from qracer.watchlist import Watchlist

    wl: Watchlist = watchlist  # type: ignore[assignment]
    if not wl.tickers:
        click.echo("Watchlist is empty. Use 'watch TICKER' to add.\n")
        return

    click.echo(f"Watchlist ({len(wl)} stocks)")
    for ticker in wl.tickers:
        click.echo(f"  {ticker}")
    click.echo()


# ---------------------------------------------------------------------------
# Task scheduling helpers
# ---------------------------------------------------------------------------


def _show_tasks(executor: object | None) -> None:
    """Display all scheduled tasks."""
    from qracer.task_executor import TaskExecutor

    if executor is None:
        click.echo("Task scheduler is not available.\n")
        return

    ex: TaskExecutor = executor  # type: ignore[assignment]
    tasks = ex.store.get_all()
    if not tasks:
        click.echo("No scheduled tasks. Use 'schedule <action> every/at <time>' to create one.\n")
        return

    active = [t for t in tasks if t.enabled]
    done = [t for t in tasks if not t.enabled]

    if active:
        click.echo(f"Active tasks ({len(active)}):")
        for t in active:
            next_run = t.next_run_at[:16] if t.next_run_at else "—"
            click.echo(f"  [{t.id}] {t.describe()}  next={next_run}  runs={t.run_count}")

    if done:
        click.echo(f"Completed/cancelled ({len(done)}):")
        for t in done:
            click.echo(f"  [{t.id}] {t.describe()}  runs={t.run_count}")

    click.echo()


def _handle_schedule_command(user_input: str, executor: object | None) -> None:
    """Parse and create a scheduled task.

    Supported formats::

        schedule analyze AAPL every 1h
        schedule news scan TSLA at 2026-04-08T09:30
        schedule portfolio snapshot daily 09:30
        schedule query "macro outlook" every 1d
    """
    from qracer.task_executor import TaskExecutor
    from qracer.tasks import TaskActionType, parse_schedule

    if executor is None:
        click.echo("Task scheduler is not available.\n")
        return

    ex: TaskExecutor = executor  # type: ignore[assignment]
    parts = user_input.split(maxsplit=1)
    if len(parts) < 2:
        click.echo(
            "Usage: schedule analyze TICKER every/at <time>\n"
            "       schedule news scan TICKER every/at <time>\n"
            "       schedule portfolio snapshot every/at <time>\n"
            '       schedule query "<text>" every/at <time>\n'
        )
        return

    body = parts[1].strip()

    # Parse action and schedule from body
    action_type: TaskActionType | None = None
    action_params: dict = {}
    schedule_spec: str = ""

    if body.startswith("analyze "):
        rest = body[len("analyze ") :]
        # "AAPL every 1h" or "AAPL at 2026-..."
        action_type = TaskActionType.ANALYZE
        action_params, schedule_spec = _split_action_schedule(rest)
        if "ticker" not in action_params:
            token = rest.split()[0] if rest.split() else ""
            action_params["ticker"] = token.upper()

    elif body.startswith("news scan "):
        rest = body[len("news scan ") :]
        action_type = TaskActionType.NEWS_SCAN
        action_params, schedule_spec = _split_action_schedule(rest)
        if "ticker" not in action_params:
            token = rest.split()[0] if rest.split() else ""
            action_params["ticker"] = token.upper()

    elif body.startswith("portfolio snapshot"):
        rest = body[len("portfolio snapshot") :].strip()
        action_type = TaskActionType.PORTFOLIO_SNAPSHOT
        action_params = {}
        schedule_spec = rest

    elif body.startswith("cross market "):
        rest = body[len("cross market ") :]
        action_type = TaskActionType.CROSS_MARKET_SCAN
        # "AAPL,TSLA every 1h"
        tokens = rest.split()
        tickers_str = tokens[0] if tokens else ""
        action_params = {"tickers": [t.strip().upper() for t in tickers_str.split(",")]}
        schedule_spec = " ".join(tokens[1:]) if len(tokens) > 1 else ""

    elif body.startswith("query "):
        rest = body[len("query ") :]
        action_type = TaskActionType.CUSTOM_QUERY
        # Extract quoted query and schedule
        if rest.startswith('"'):
            end_quote = rest.find('"', 1)
            if end_quote > 0:
                action_params = {"query": rest[1:end_quote]}
                schedule_spec = rest[end_quote + 1 :].strip()
            else:
                click.echo("Missing closing quote for query.\n")
                return
        else:
            click.echo('Usage: schedule query "your question" every/at <time>\n')
            return

    if action_type is None or not schedule_spec:
        click.echo(
            "Could not parse schedule command.\nUsage: schedule analyze TICKER every/at <time>\n"
        )
        return

    # Strip leading "at " for one-time schedules
    if schedule_spec.startswith("at "):
        schedule_spec = schedule_spec[3:]

    try:
        parse_schedule(schedule_spec)
    except ValueError as e:
        click.echo(f"Invalid schedule: {e}\n")
        return

    task = ex.store.create(action_type, action_params, schedule_spec)
    click.echo(f"Task scheduled: {task.describe()} [{task.id}]\n")


def _split_action_schedule(text: str) -> tuple[dict, str]:
    """Split 'TICKER every 1h' into ({"ticker": "TICKER"}, "every 1h")."""
    for keyword in (" every ", " at ", " daily ", " weekly "):
        idx = text.lower().find(keyword)
        if idx >= 0:
            ticker = text[:idx].strip().upper()
            spec = text[idx:].strip()
            if spec.startswith("at "):
                spec = spec[3:]
            return {"ticker": ticker}, spec
    return {}, text


def _handle_cancel_task(user_input: str, executor: object | None) -> None:
    """Cancel a task by ID."""
    from qracer.task_executor import TaskExecutor

    if executor is None:
        click.echo("Task scheduler is not available.\n")
        return

    ex: TaskExecutor = executor  # type: ignore[assignment]
    parts = user_input.split()
    if len(parts) < 2:
        click.echo("Usage: cancel-task ID\n")
        return

    task_id = parts[1]
    if ex.store.cancel(task_id):
        click.echo(f"Task {task_id} cancelled.\n")
    else:
        click.echo(f"No task found with ID {task_id}.\n")


@main.command()
def repl() -> None:
    """Start interactive conversational session."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    import uuid

    from qracer.alert_monitor import AlertMonitor
    from qracer.alerts import AlertStore
    from qracer.conversation.engine import ConversationEngine
    from qracer.memory.memory_searcher import MemorySearcher
    from qracer.memory.session_logger import SessionLogger
    from qracer.watchlist import Watchlist

    llm_registry, data_registry, provider_warnings = _build_registries()

    # Surface provider warnings so the user knows what's unavailable.
    for warn in provider_warnings:
        click.echo(f"  ⚠ {warn}")
    if provider_warnings:
        click.echo()

    # Apply config-driven pipeline defaults.
    from qracer.config.loader import load_config
    from qracer.tools.pipeline import configure as configure_pipeline

    app_cfg = load_config().app
    configure_pipeline(
        lookback_days=app_cfg.lookback_days,
        staleness_hours=app_cfg.staleness_hours,
    )

    # Create session logger in ~/.qracer/sessions/<uuid>.jsonl
    sessions_dir = _user_dir() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex[:12]
    session_logger = SessionLogger(sessions_dir / f"{session_id}.jsonl")

    # Cross-session memory (Tier 2 summaries + Tier 3 search index).
    summaries_dir = _user_dir() / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    memory_searcher = MemorySearcher(_user_dir() / "memory_index.duckdb")
    loaded_contexts = memory_searcher.index_directory(summaries_dir)
    if loaded_contexts:
        click.echo(f"  ✓ Loaded {loaded_contexts} past session summaries from {summaries_dir}")

    from qracer.memory.fact_store import FactStore

    fact_store = FactStore(_user_dir() / "fact_store.duckdb")
    open_theses = fact_store.get_open_theses()
    if open_theses:
        click.echo(f"  ✓ Loaded {len(open_theses)} open theses from fact store")

    reports_dir = _user_dir() / "reports"
    watchlist = Watchlist(_user_dir() / "watchlist.json")

    # Price alerts
    alert_store = AlertStore(_user_dir() / "alerts.json")
    alert_monitor = AlertMonitor(alert_store, data_registry)

    # Task scheduler
    from qracer.task_executor import TaskExecutor
    from qracer.tasks import TaskStore

    task_store = TaskStore(_user_dir() / "tasks.json")

    engine = ConversationEngine(
        llm_registry,
        data_registry,
        max_iterations=app_cfg.max_iterations,
        confidence_threshold=app_cfg.confidence_threshold,
        session_logger=session_logger,
        report_dir=reports_dir,
        language=app_cfg.language,
        memory_searcher=memory_searcher,
        summaries_dir=summaries_dir,
        fact_store=fact_store,
        context_path=_user_dir() / "context.json",
    )

    task_executor = TaskExecutor(task_store, data_registry, llm_registry, engine=engine)

    asyncio.run(
        _repl_loop(
            engine,
            watchlist,
            alert_monitor=alert_monitor,
            task_executor=task_executor,
            data_registry=data_registry,
            sessions_dir=sessions_dir,
            current_session=session_logger.path,
            fact_store=fact_store,
        )
    )


# ---------------------------------------------------------------------------
# qracer serve
# ---------------------------------------------------------------------------


@main.command()
@click.option("--check-interval", default=30, help="Seconds between task/alert checks.")
def serve(check_interval: int) -> None:
    """Run qracer as a background service (scheduled tasks + alerts)."""
    import signal

    from qracer.alert_monitor import AlertMonitor
    from qracer.alerts import AlertStore
    from qracer.notifications.factory import (
        build_notification_registry,
        build_telegram_poller,
    )
    from qracer.pidfile import acquire, release
    from qracer.server import Server
    from qracer.task_executor import TaskExecutor
    from qracer.tasks import TaskStore

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    pid_path = _user_dir() / "serve.pid"
    if not acquire(pid_path):
        click.echo("qracer serve is already running. Use 'qracer serve-stop' or check serve.pid.")
        sys.exit(1)

    llm_registry, data_registry, provider_warnings = _build_registries()
    for warn in provider_warnings:
        click.echo(f"  ⚠ {warn}")

    # Apply config-driven pipeline defaults.
    from qracer.tools.pipeline import configure as configure_pipeline

    app_cfg = load_config().app
    configure_pipeline(
        lookback_days=app_cfg.lookback_days,
        staleness_hours=app_cfg.staleness_hours,
    )

    alert_store = AlertStore(_user_dir() / "alerts.json")
    alert_monitor = AlertMonitor(alert_store, data_registry, check_interval=check_interval)

    task_store = TaskStore(_user_dir() / "tasks.json")
    task_executor = TaskExecutor(
        task_store, data_registry, llm_registry, check_interval=check_interval
    )

    config = load_config()
    notifications = build_notification_registry(config.credentials)
    telegram_poller = build_telegram_poller(config.credentials)

    # Autonomous market monitoring
    from qracer.autonomous import AutonomousMonitor
    from qracer.watchlist import Watchlist

    autonomous_monitor: AutonomousMonitor | None = None
    if app_cfg.autonomous_enabled:
        watchlist = Watchlist(_user_dir() / "watchlist.json")
        autonomous_monitor = AutonomousMonitor(
            watchlist,
            data_registry,
            check_interval=check_interval,
            price_threshold_pct=app_cfg.price_move_threshold_pct,
            cooldown_minutes=app_cfg.alert_cooldown_minutes,
        )

    streaming_adapter = _build_streaming_adapter(config)

    server = Server(
        alert_monitor,
        task_executor,
        notifications,
        autonomous_monitor=autonomous_monitor,
        telegram_poller=telegram_poller,
        streaming_adapter=streaming_adapter,
        tick_interval=1.0,
    )

    def _handle_signal(signum: int, _frame: object) -> None:
        click.echo(f"\nReceived signal {signum}, shutting down...")
        server.shutdown()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    click.echo(f"qracer serve started (check every {check_interval}s, PID {os.getpid()})")
    channels = notifications.channels
    if channels:
        click.echo(f"  Notifications: {', '.join(channels)}")
    if autonomous_monitor:
        click.echo(
            f"  Autonomous monitoring: threshold={app_cfg.price_move_threshold_pct}%,"
            f" cooldown={app_cfg.alert_cooldown_minutes}m"
        )
    if telegram_poller is not None:
        click.echo("  Telegram bot: receiving commands (try /help in chat)")
    if streaming_adapter is not None:
        click.echo("  Streaming: Finnhub WebSocket (real-time alerts)")
    click.echo("  Press Ctrl+C to stop.\n")

    try:
        asyncio.run(server.run())
    finally:
        release(pid_path)
        click.echo("qracer serve stopped.")


# ---------------------------------------------------------------------------
# qracer dashboard
# ---------------------------------------------------------------------------


@main.command()
def dashboard() -> None:
    """Launch the interactive TUI dashboard."""
    try:
        from qracer.dashboard.app import QracerDashboard
    except ImportError:
        click.echo(
            "Dashboard requires the 'textual' package.\n"
            "Install it with: pip install 'qracer[dashboard]'"
        )
        raise SystemExit(1)

    from qracer.alerts import AlertStore
    from qracer.tasks import TaskStore

    _, data_registry, provider_warnings = _build_registries()
    for warn in provider_warnings:
        click.echo(f"  ⚠ {warn}")

    alert_store = AlertStore(_user_dir() / "alerts.json")
    task_store = TaskStore(_user_dir() / "tasks.json")

    app = QracerDashboard(
        data_registry=data_registry,
        alert_store=alert_store,
        task_store=task_store,
    )
    app.run()


# ---------------------------------------------------------------------------
# qracer web
# ---------------------------------------------------------------------------


@main.command()
@click.option("--host", default="127.0.0.1", help="Host interface to bind to.")
@click.option("--port", default=8000, type=int, help="TCP port to listen on.")
def web(host: str, port: int) -> None:
    """Launch the FastAPI web dashboard API."""
    try:
        import uvicorn

        from qracer.web.app import create_app
    except ImportError:
        click.echo(
            "Web dashboard requires FastAPI and uvicorn.\n"
            "Install them with: pip install 'qracer[web]'"
        )
        raise SystemExit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    app = create_app()
    click.echo(f"qracer web API listening on http://{host}:{port}")
    click.echo("  Routes mounted under /api (try /api/health)")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
