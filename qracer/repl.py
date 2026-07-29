"""Interactive REPL loop and its slash-command handlers.

Extracted from cli.py to separate the interactive layer from the thin Click
command layer. The ``qracer repl`` command wires up the engine + stores and hands
them to :func:`_repl_loop`; the ``run`` / ``brief`` / ``serve`` commands reuse the
agent- and briefing-building helpers here. Heavy imports stay inside the functions.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from qracer.bootstrap import build_registries
from qracer.config.loader import _user_dir, load_config

logger = logging.getLogger(__name__)

# Kept for the moved call sites that referenced the cli.py alias.
_build_registries = build_registries

BANNER = """\
╔══════════════════════════════════════════╗
║  qracer — conversational alpha engine   ║
╚══════════════════════════════════════════╝
Type your query, or 'quit' to exit.
Commands: save, save json, save pdf, backtest, help
"""


def _openrouter_provider(llm_registry: object) -> object | None:
    """Resolve the OpenRouter LLM adapter from a registry, or None if unavailable.

    Custom agents address OpenRouter model ids directly, so they run through the
    single OpenRouter adapter (which honors ``CompletionRequest.model``).
    """
    from qracer.llm.providers import Role

    try:
        return llm_registry.get(Role.RESEARCHER, name="openrouter")  # type: ignore[attr-defined]
    except KeyError:
        return None


def _build_briefing_composer(
    data_registry: object, llm_registry: object, fact_store: object | None = None
) -> object:
    """Construct a BriefingComposer from the user's portfolio, watchlist, and fact store.

    Reuses *fact_store* when given — DuckDB refuses a second read-write handle to the
    same file in one process, so callers that already opened a FactStore (e.g. the
    REPL) must pass it in rather than let this open another.
    """
    from qracer.briefing import BriefingComposer
    from qracer.memory.fact_store import FactStore
    from qracer.watchlist import Watchlist

    cfg = load_config()
    fs = fact_store if fact_store is not None else FactStore(_user_dir() / "fact_store.duckdb")
    return BriefingComposer(
        data_registry,  # type: ignore[arg-type]
        llm_registry,  # type: ignore[arg-type]
        cfg.portfolio,
        Watchlist(_user_dir() / "watchlist.json"),
        fs,  # type: ignore[arg-type]
        top_n=cfg.app.briefing.top_n,
    )


_HELP_TEXT = """\
Available commands:
  save              Save last analysis as Markdown
  save json         Save last analysis as JSON
  save pdf          Save last analysis as PDF (requires qracer[pdf] extra)
  backtest          Backtest the last trade thesis against historical data
  brief             AI-prioritized briefing from your portfolio/watchlist
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
    agent_store: object | None = None,
    agent_provider: object | None = None,
    briefing_composer: object | None = None,
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

        # Custom agents: run all enabled agents now (ad-hoc), ignoring schedule.
        # Slash-form only: "run" is a common query verb ("run a DCF on TSLA").
        if cmd == "/run" or cmd.startswith("/run "):
            await _handle_run_agents(user_input, agent_store, agent_provider)
            continue

        # Daily briefing preview (AI-prioritized).
        if cmd in ("brief", "/brief"):
            await _handle_brief(briefing_composer)
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


def _report_agent_results(results: object, store: object) -> None:
    """Print each agent result and persist it so the web UI can show it."""
    for r in results:  # type: ignore[attr-defined]
        click.echo(f"\n── {r.name} [{r.model}] ──")
        click.echo(r.output if r.ok else f"(error) {r.error}")
        store.record_run(  # type: ignore[attr-defined]
            r.agent_id, output=r.output if r.ok else None, error=r.error
        )


async def _handle_run_agents(
    user_input: str,
    agent_store: object | None,
    agent_provider: object | None,
) -> None:
    """Run all enabled custom agents now, ignoring their schedule.

    ``/run`` runs every enabled agent with the default trigger message;
    ``/run <text>`` passes <text> to each agent as the user turn.
    """
    from qracer.agent_runner import run_agents
    from qracer.agents_store import AgentStore

    if not isinstance(agent_store, AgentStore) or agent_provider is None:
        click.echo("Custom agents unavailable (OpenRouter not configured).\n")
        return

    parts = user_input.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else None

    agents = [a for a in agent_store.agents if a.enabled]
    if not agents:
        click.echo("No enabled agents. Add some via 'qracer web'.\n")
        return

    click.echo(f"Running {len(agents)} agent(s)...")
    results = await run_agents(agents, agent_provider, user_input=query)  # type: ignore[arg-type]
    _report_agent_results(results, agent_store)
    click.echo()


async def _handle_brief(briefing_composer: object | None) -> None:
    """Compose and print an AI-prioritized briefing on demand."""
    if briefing_composer is None:
        click.echo("Briefing unavailable.\n")
        return
    click.echo("Composing briefing...")
    text = await briefing_composer.compose()  # type: ignore[attr-defined]
    click.echo("\r" + " " * 20 + "\r", nl=False)
    click.echo(
        (text or "No briefing yet — add holdings/watchlist tickers or run some analyses.") + "\n"
    )


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
