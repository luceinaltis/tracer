# qracer

Conversational investment agent for discovering alpha in global security markets.

Natural language queries → cross-market analysis → actionable alpha reports with sized recommendations.

## Features

- **Conversational pipeline** — intent → handler → tools → LLM synthesis, with
  quick lookups, deep analysis, multi-ticker comparison, and portfolio checks
- **Capability-routed data** — yfinance (price), Finnhub (fundamentals/news/insider),
  FRED (macro), DART (Korean company financials/disclosures, by 6-digit KRX code);
  pluggable via a capability registry + entry points
- **Role-based LLM routing** — Researcher / Analyst / Strategist / Reporter roles
- **Portfolio-aware risk** — position sizing by conviction, sector/correlation limits, rebalance
- **Session memory** — 3-tier (JSONL audit → compressed summaries → DuckDB search index)
- **Autonomous `serve`** — scheduled tasks, price alerts, market-hours monitor, and
  prompt-defined custom agents

## Quick Start

```bash
# Install dependencies
uv sync

# First-time setup (creates ~/.qracer/ config)
qracer install

# Start interactive session
qracer repl
```

### Configuration

Settings live in `~/.qracer/` and can be edited from the terminal or the web dashboard —
both share one schema and a comment-preserving writer, so nothing hand-edited is lost:

```bash
qracer config                       # list all settings, grouped, with current values
qracer config set briefing.schedule "0 9 * * *"
qracer config providers             # enable/disable providers and set API keys, interactively
```

Or open the web dashboard's **Settings** tab (`qracer web`, localhost only) to toggle
providers, enter API keys (masked), and change app/briefing/portfolio settings from a form.

## Architecture

```text
CLI (REPL)
    ↓
ConversationEngine
    ↓
IntentParser → handler
    ├─ QuickPathHandler    — template lookup (no LLM)
    ├─ ComparisonHandler   — per-ticker fan-out + table
    ├─ PortfolioHandler    — snapshot + limit check
    └─ StandardHandler     — deep path:
           invoke_tools → AnalysisLoop → trade_thesis → risk_check → ResponseSynthesizer
```

Data and LLM providers register capabilities/roles via a **Registry pattern** — tools
request by capability, not by source. See [docs/architecture.md](docs/architecture.md).

## Project Structure

```text
qracer/
├── conversation/   # Intent parsing, context, engine, handlers, analysis loop, synthesizer
├── data/           # Capability protocols + adapters (yfinance, Finnhub, FRED, DART) + registry
├── llm/            # LLM role routing + adapters (Claude, OpenAI, Gemini, OpenRouter)
├── risk/           # Portfolio risk calculator, position sizing, correlation
├── memory/         # Session logging, compaction, search, fact store
├── tools/          # Pipeline tool wrappers (ToolResult)
├── config/         # .qracer/ config loading and pydantic models
├── notifications/  # Telegram send + inbound poller
├── web/            # FastAPI API + NiceGUI dashboard (status + editable Agents/Settings)
├── models/         # Domain models (ToolResult, TradeThesis)
├── agents/         # Legacy role classes (not wired into the live pipeline)
├── server.py       # `qracer serve` daemon loop
├── task_executor.py, tasks.py, alerts.py, autonomous.py  # scheduled tasks / alerts / monitor
└── agents_store.py, agent_runner.py, agent_monitor.py    # custom autonomous agents
```

## Configuration

Settings live in `.qracer/` (project-local or `~/.qracer/`):

```text
.qracer/
├── config.toml        # Global settings (default mode, LLM preferences)
├── providers.toml     # Data source config (enabled, priority, tier)
├── portfolio.toml     # Watchlist, holdings, risk limits
└── credentials.env    # API keys (user-level only, gitignored)
```

## Development

```bash
# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run pyright

# Test with coverage (80% minimum)
uv run pytest --cov=qracer --cov-report=term-missing --cov-fail-under=80
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12+ |
| Package manager | uv |
| Lint / Format | ruff |
| Type checker | pyright |
| Test | pytest + pytest-asyncio |
| LLM | Multi-provider (Claude, OpenAI, Gemini, OpenRouter) |
| Data | Multi-source (yfinance, Finnhub, FRED, DART) |
| Storage | DuckDB + file-backed JSON stores |

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | Layer map, capability registry, config/storage, roadmap |
| [Pipeline](docs/pipeline.md) | Query → handler → tools → thesis → synthesis |
| [Conversational Layer](docs/conversational-layer.md) | Intent, context, roles, response formats |
| [Risk System](docs/risk-system.md) | Portfolio model, position sizing, exposure limits |
| [Memory System](docs/memory-system.md) | 3-tier session memory architecture |
| [Custom Agents](docs/custom-agents.md) | Prompt-defined autonomous agents (cron/continuous) |
| [Serve](docs/serve.md) | Background daemon: tasks, alerts, monitors |
| [Schedule](docs/schedule.md) | Task scheduler |
