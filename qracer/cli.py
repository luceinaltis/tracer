"""CLI entrypoint — Click-based commands for qracer."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from qracer.bootstrap import build_registries
from qracer.config import writer
from qracer.config.loader import (
    _load_toml,
    _user_dir,
    load_config,
    resolve_config_dirs,
)
from qracer.repl import (
    _build_briefing_composer,
    _openrouter_provider,
    _repl_loop,
    _report_agent_results,
)

if TYPE_CHECKING:
    from qracer.config.models import QracerConfig
    from qracer.data.providers import StreamingProvider

logger = logging.getLogger(__name__)

# Windows consoles often default to a legacy code page (e.g. cp949 on Korean
# Windows) that cannot encode the box-drawing / ✓ / — glyphs this CLI prints,
# which raises UnicodeEncodeError mid-output. Force UTF-8 on the streams before
# Click emits anything (help text is printed during argument parsing).
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")

SCHEMA_DIR = Path(__file__).parent / "config" / "schema"


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
    "openrouter": "OpenRouter (multi-model gateway)",
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


def _select_openrouter_model() -> str:
    """Interactive model picker backed by the live OpenRouter ``/models`` catalog.

    Search → numbered list → pick.  Falls back to manual entry if the catalog
    cannot be fetched (offline), and accepts a typed model id at any prompt.
    """
    from qracer.llm.openrouter_adapter import DEFAULT_OPENROUTER_MODEL, list_models

    click.echo("\n  Fetching available OpenRouter models...")
    while True:
        term = click.prompt(
            "  Search models (keyword like 'claude'/'gpt', or blank to browse all)",
            default="",
            show_default=False,
        ).strip()
        try:
            matches = list_models(term or None)
        except Exception as exc:  # network / parse failure → manual entry
            click.echo(f"  ⚠ Could not fetch model list ({exc}).")
            return click.prompt(
                "  Enter an OpenRouter model id manually", default=DEFAULT_OPENROUTER_MODEL
            ).strip()

        if not matches:
            click.echo("  No models matched — try another keyword.")
            continue

        shown = matches[:25]
        suffix = f" matching '{term}'" if term else ""
        click.echo(f"\n  {len(matches)} model(s){suffix}:")
        for i, m in enumerate(shown, 1):
            click.echo(
                f"    {i:2}. {m.id}  "
                f"(ctx {m.context_length:,}, "
                f"${m.prompt_per_million:.2f}/${m.completion_per_million:.2f} per 1M in/out)"
            )
        if len(matches) > len(shown):
            click.echo(f"    ... {len(matches) - len(shown)} more — narrow with a keyword.")

        choice = click.prompt(
            "  Pick a number, type a model id, or 's' to search again", default="1"
        ).strip()
        if choice.lower() == "s":
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(shown):
                return shown[idx - 1].id
            click.echo("  Number out of range — try again.")
            continue
        # Any other input is treated as a literal model id.
        return choice


@main.command()
def install() -> None:
    """Interactive first-time setup wizard."""
    home_dir = _user_dir()
    click.echo(f"Setting up qracer config in {home_dir}\n")

    # 1. Create directory
    home_dir.mkdir(parents=True, exist_ok=True)

    # 2. Copy default config files for any that don't exist yet (existing files are
    #    left in place; the wizard's choices below are applied as surgical, comment-
    #    preserving upserts via the writer, so re-running install is safe).
    for name in ("config.toml", "providers.toml", "portfolio.toml"):
        dest = home_dir / name
        if dest.exists():
            click.echo(f"  {name} already exists, keeping it.")
        else:
            shutil.copy2(SCHEMA_DIR / name, dest)
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

    # 4b. OpenRouter: choose a model from the live catalog.
    openrouter_model: str | None = None
    if chosen_name == "openrouter":
        openrouter_model = _select_openrouter_model()

    # 5. Prompt for portfolio currency
    currency = click.prompt("\n  Portfolio currency", default="USD").strip()

    # 6. Write credentials.env (structured upsert, preserves any existing keys).
    if chosen_env and creds.get(chosen_env):
        writer.set_credential(chosen_env, creds[chosen_env], config_dir=home_dir)
    (home_dir / "credentials.env").touch(exist_ok=True)  # always present, even if empty

    # 7. Enable the chosen LLM provider, disable the others (surgical upsert).
    for name, _, _ in llm_choices:
        writer.set_provider_field(name, "enabled", name == chosen_name, config_dir=home_dir)

    # 7b. Persist provider (+ OpenRouter model) choice to config.toml.
    writer.set_config_value("llm_provider", chosen_name, config_dir=home_dir)
    if openrouter_model:
        writer.set_config_value("llm_model", openrouter_model, config_dir=home_dir)
        click.echo(f"  Model set to {openrouter_model}")

    # 8. Update portfolio currency if non-default.
    if currency and currency != "USD":
        writer.set_portfolio_value("currency", currency, config_dir=home_dir)

    click.echo(f"\n✓ Setup complete! {chosen_label} enabled as LLM provider.")

    # Warn if API key was not provided
    if chosen_env and not creds.get(chosen_env) and not os.environ.get(chosen_env):
        click.echo(f"\n⚠ {chosen_env} not set — set it before running 'qracer repl'.")

    click.echo("\nNext steps:")
    click.echo("  qracer status   — check configuration")
    click.echo("  qracer repl     — start interactive session")


# ---------------------------------------------------------------------------
# qracer models
# ---------------------------------------------------------------------------


@main.command()
@click.option("--search", "-s", default=None, help="Filter models by keyword (id or name).")
@click.option("--limit", "-n", "limit", default=50, show_default=True, help="Max models to show.")
def models(search: str | None, limit: int) -> None:
    """List available OpenRouter models (id, context window, price)."""
    from qracer.llm.openrouter_adapter import list_models

    try:
        found = list_models(search)
    except Exception as exc:
        click.echo(f"Failed to fetch OpenRouter models: {exc}")
        raise SystemExit(1) from exc

    header = f"{len(found)} model(s)"
    if search:
        header += f" matching '{search}'"
    click.echo(header + ":")
    for m in found[:limit]:
        click.echo(
            f"  {m.id}  "
            f"(ctx {m.context_length:,}, "
            f"${m.prompt_per_million:.2f}/${m.completion_per_million:.2f} per 1M in/out)"
        )
    if len(found) > limit:
        click.echo(f"  ... and {len(found) - limit} more — use --search or --limit.")


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


@main.group("config", invoke_without_command=True)
@click.option("--set", "set_pair", default=None, help="(compat) Set a value: key=value")
@click.pass_context
def config_cmd(ctx: click.Context, set_pair: str | None) -> None:
    """Show or update configuration.

    Bare `qracer config` lists all settings. Use `config set <key> <value>` to
    change one, `config get <key>` to read one, and `config providers` to
    enable/disable providers and set their API keys interactively.
    """
    if set_pair is not None:  # backward-compatible `config --set key=value`
        if ctx.invoked_subcommand is not None:
            raise click.UsageError("--set cannot be combined with a subcommand.")
        if "=" not in set_pair:
            raise click.BadParameter("Expected key=value format", param_hint="--set")
        key, _, value = set_pair.partition("=")
        _apply_setting(key.strip(), value.strip())
        return
    if ctx.invoked_subcommand is None:
        _show_settings()


def _apply_setting(key: str, raw_value: str) -> None:
    """Validate *raw_value* against the schema and persist it via the config writer."""
    from qracer.config import settings_schema as ss

    setting = ss.find(key)
    if setting is None:
        raise click.ClickException(
            f"Unknown setting {key!r}. Run 'qracer config' to see available keys."
        )
    try:
        value = ss.coerce(setting, raw_value)
        ss.validate(setting, value)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if setting.target == "config":
        writer.set_config_value(setting.key, value)
    else:
        writer.set_portfolio_value(setting.key, value)
    click.echo(f"Set {setting.key} = {value}")


@config_cmd.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a setting: qracer config set <key> <value>."""
    _apply_setting(key, value)


@config_cmd.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Print the current value of a setting."""
    from qracer.config import settings_schema as ss

    setting = ss.find(key)
    if setting is None:
        raise click.ClickException(f"Unknown setting {key!r}.")
    click.echo(ss.get_current(load_config(force_reload=True), setting))


@config_cmd.command("providers")
def config_providers() -> None:
    """Interactively enable/disable providers and set their API keys."""
    from qracer.config import settings_schema as ss

    while True:
        rows = ss.provider_settings(load_config(force_reload=True))
        click.echo("\nProviders:")
        for r in rows:
            state = "on " if r.enabled else "off"
            key = "" if not r.api_key_env else (" key:set" if r.has_key else " key:missing")
            click.echo(f"  [{state}] {r.name} ({r.kind}, prio {r.priority}){key}")
        name = click.prompt(
            "\nProvider to configure (blank to finish)", default="", show_default=False
        ).strip()
        if not name:
            break
        row = next((r for r in rows if r.name == name), None)
        if row is None:
            click.echo(f"  Unknown provider {name!r}.")
            continue
        enable = click.confirm(f"  Enable {name}?", default=row.enabled)
        writer.set_provider_field(name, "enabled", enable)
        if row.api_key_env:
            entered = click.prompt(
                f"  {row.api_key_env} (blank keeps current)", default="", show_default=False
            ).strip()
            if entered:
                writer.set_credential(row.api_key_env, entered)
        click.echo(f"  Saved {name}.")
    click.echo("Done.")


def _show_settings() -> None:
    """Print all settings grouped by section, with current values (keys masked)."""
    from qracer.config import settings_schema as ss

    cfg = load_config(force_reload=True)
    for group in ss.groups():
        click.echo(f"\n{group}:")
        for s in ss.APP_SETTINGS:
            if s.group == group:
                click.echo(f"  {s.key} = {ss.get_current(cfg, s)}")
    click.echo("\nProviders:")
    for r in ss.provider_settings(cfg):
        state = "on " if r.enabled else "off"
        key = "" if not r.api_key_env else (" key:set" if r.has_key else " key:missing")
        click.echo(f"  [{state}] {r.name} ({r.kind}, prio {r.priority}){key}")
    click.echo("\nEdit with:  qracer config set <key> <value>  |  qracer config providers")


# ---------------------------------------------------------------------------
# qracer repl
# ---------------------------------------------------------------------------


# Composition root moved to qracer.bootstrap so the web app can reuse it without a
# cli ↔ web import cycle. Keep the private alias for the existing call sites.
_build_registries = build_registries


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

    # Custom agents (model + free-form prompt) for on-demand `/run`.
    from qracer.agents_store import AgentStore

    agent_store = AgentStore(_user_dir() / "agents.json")
    agent_provider = _openrouter_provider(llm_registry)
    # Reuse the FactStore already opened above — a second DuckDB read-write handle to
    # the same file in one process raises a lock conflict.
    briefing_composer = _build_briefing_composer(data_registry, llm_registry, fact_store)

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
            agent_store=agent_store,
            agent_provider=agent_provider,
            briefing_composer=briefing_composer,
        )
    )


# ---------------------------------------------------------------------------
# qracer run
# ---------------------------------------------------------------------------


@main.command()
@click.argument("query", required=False)
@click.option("--agent", "agent_name", default=None, help="Run only the agent with this name.")
def run(query: str | None, agent_name: str | None) -> None:
    """Run custom agents now (ad-hoc), ignoring their schedule.

    Runs every enabled agent, or just ``--agent NAME``. An optional QUERY is
    passed to each agent as the user turn (otherwise the default trigger is used).
    """
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    from qracer.agent_runner import run_agents
    from qracer.agents_store import AgentStore

    llm_registry, _data, warnings = _build_registries()
    for warn in warnings:
        click.echo(f"  ⚠ {warn}")

    provider = _openrouter_provider(llm_registry)
    if provider is None:
        click.echo(
            "OpenRouter is not configured. Enable it in providers.toml and set "
            "OPENROUTER_API_KEY, then retry."
        )
        sys.exit(1)

    store = AgentStore(_user_dir() / "agents.json")
    if agent_name:
        agent = store.find_by_name(agent_name)
        if agent is None:
            click.echo(f"No agent named {agent_name!r}. See 'qracer web' to manage agents.")
            sys.exit(1)
        agents = [agent]
    else:
        agents = [a for a in store.agents if a.enabled]

    if not agents:
        click.echo("No agents to run. Add some via 'qracer web'.")
        return

    click.echo(f"Running {len(agents)} agent(s)...")
    results = asyncio.run(run_agents(agents, provider, user_input=query))  # type: ignore[arg-type]
    _report_agent_results(results, store)


# ---------------------------------------------------------------------------
# qracer brief
# ---------------------------------------------------------------------------


@main.command()
@click.option("--send", is_flag=True, help="Also push to configured notification channels.")
def brief(send: bool) -> None:
    """Compose an AI-prioritized briefing from your portfolio/watchlist and print it now."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    llm_registry, data_registry, warnings = _build_registries()
    for warn in warnings:
        click.echo(f"  ⚠ {warn}")

    composer = _build_briefing_composer(data_registry, llm_registry)
    text = asyncio.run(composer.compose())  # type: ignore[attr-defined]
    if not text:
        click.echo(
            "No briefing yet — add holdings to portfolio.toml, tickers to your watchlist, "
            "or run some analyses first."
        )
        return
    click.echo(text)

    if send:
        from qracer.notifications.factory import build_notification_registry
        from qracer.notifications.providers import Notification, NotificationCategory

        cfg = load_config()
        notifications = build_notification_registry(cfg.credentials)
        if not notifications.channels:
            click.echo("\n(no notification channels configured — not sent)")
        else:
            asyncio.run(
                notifications.notify(
                    Notification(
                        category=NotificationCategory.DAILY_BRIEFING,
                        title="Daily briefing",
                        body=text,
                    )
                )
            )
            click.echo(f"\n(sent via {', '.join(notifications.channels)})")


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

    # Custom autonomous agents (model + free-form prompt, cron/continuous).
    from qracer.agent_monitor import AgentMonitor
    from qracer.agents_store import AgentStore

    agent_monitor: AgentMonitor | None = None
    agent_store = AgentStore(_user_dir() / "agents.json")
    agent_provider = _openrouter_provider(llm_registry)
    if agent_provider is not None:
        agent_monitor = AgentMonitor(agent_store, agent_provider)  # type: ignore[arg-type]
    elif len(agent_store) > 0:
        click.echo("  ⚠ Custom agents found but OpenRouter is not configured — agents disabled.")

    # AI-prioritized daily briefing (pushed on a cron schedule).
    briefing_scheduler = None
    if app_cfg.briefing.enabled:
        from qracer.briefing import BriefingScheduler

        tz = None
        if app_cfg.briefing.timezone:
            from zoneinfo import ZoneInfo

            try:
                tz = ZoneInfo(app_cfg.briefing.timezone)
            except Exception:
                click.echo(f"  ⚠ Unknown briefing timezone: {app_cfg.briefing.timezone}")
        composer = _build_briefing_composer(data_registry, llm_registry)
        try:
            briefing_scheduler = BriefingScheduler(
                composer,  # type: ignore[arg-type]
                notifications,
                app_cfg.briefing.schedule,
                timezone=tz,
            )
        except Exception as exc:  # invalid cron
            click.echo(f"  ⚠ Briefing schedule invalid ({exc}) — briefing disabled.")

    # Real-time price streaming (WebSocket) — falls back to REST polling if unavailable.
    streaming_adapter = _build_streaming_adapter(config)

    server = Server(
        alert_monitor,
        task_executor,
        notifications,
        autonomous_monitor=autonomous_monitor,
        agent_monitor=agent_monitor,
        briefing_scheduler=briefing_scheduler,
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
    if agent_monitor is not None:
        scheduled = sum(
            1 for a in agent_store.agents if a.enabled and a.trigger_type.value != "manual"
        )
        click.echo(f"  Custom agents: {scheduled} autonomous (cron/continuous) active")
    if briefing_scheduler is not None:
        click.echo(f"  Daily briefing: schedule '{app_cfg.briefing.schedule}'")
    if streaming_adapter is not None:
        click.echo("  Streaming: Finnhub WebSocket (real-time alerts)")
    click.echo("  Press Ctrl+C to stop.\n")

    from qracer.provider_lifecycle import shutdown_all_providers_sync

    try:
        asyncio.run(server.run())
    finally:
        try:
            shutdown_all_providers_sync(data_registry, llm_registry)
        except Exception:
            logger.debug("Provider shutdown raised", exc_info=True)
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
    click.echo(f"qracer web listening on http://{host}:{port}")
    click.echo(f"  Custom-agent config UI: http://{host}:{port}/")
    click.echo("  JSON API mounted under /api (try /api/health)")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
