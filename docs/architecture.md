# Architecture

qracer is a conversational, CLI-first investment-research tool. Natural-language
queries are parsed into intents, routed to a handler, grounded with data fetched
through a capability registry, and synthesized into a response by an LLM.

This document describes what the code **actually does today**. For where the design
is heading (a grounded, source-cited "investor harness"), see the roadmap note at
the end.

## Layers

```text
CLI (qracer/cli.py)
    │  commands: install · repl · run · serve · web · dashboard · models · status
    ▼
ConversationEngine (conversation/engine.py)
    │  intent parsing · context · routing · fact persistence · compaction
    ▼
Handlers (conversation/handlers.py)
    │  PortfolioHandler · QuickPathHandler · ComparisonHandler · StandardHandler
    ▼
Dispatcher + Tools (conversation/dispatcher.py, tools/pipeline.py)
    │  price_event · news · insider · macro · fundamentals · cross_market
    │  trade_thesis · risk_check · memory_search
    ▼
Registries
    ├── DataRegistry  (data/registry.py)  — capability routing + fallback
    └── LLMRegistry   (llm/registry.py)   — role routing
```

The composition root is `_build_registries()` (`cli.py`): it reads config, discovers
providers from `provider_catalog.py`, resolves API keys, and registers each enabled
adapter into the two registries. `repl()` wires the engine and supporting subsystems
by hand.

## Capability Registry Pattern

Adapters implement capability **protocols**; the registry routes by capability, so
tools never reference a specific source. `DataRegistry` supports ordered fallback
(`async_get_with_fallback`) — register two adapters for the same capability and the
second is tried when the first fails.

```python
# data/providers.py — capability protocols
class PriceProvider(Protocol):
    async def get_price(self, ticker: str) -> float: ...
    async def get_ohlcv(self, ticker, start, end) -> list[OHLCV]: ...

# tools request by capability, not by source
registry.get(PriceProvider)          # highest-priority adapter
```

Current capabilities: `PriceProvider`, `FundamentalProvider`, `MacroProvider`,
`NewsProvider`, `AlternativeProvider` (insider). Wired adapters:

| Capability | Adapter | Notes |
|---|---|---|
| Price / OHLCV | yfinance (`data/yfinance_adapter.py`) | unofficial; IP-block prone |
| Fundamentals / News / Insider | Finnhub (`data/finnhub_adapter.py`) | US market; news sentiment not populated |
| Macro | FRED (`data/fred_adapter.py`) | 6 named series + raw series id |
| Fundamentals / Disclosures / Insider | DART (`data/dart_adapter.py`) | Korea (OpenDART); ticker = 6-digit KRX code (e.g. `005930`) |

DART and Finnhub both serve the fundamentals/news/insider capabilities: DART is
higher priority but fails fast on non-6-digit tickers, so Korean codes route to DART
and everything else falls through to Finnhub. This is the fallback machinery in
actual use. Adding a source = a capability adapter + one entry in
`provider_catalog.py` **or** an external package on the `qracer.data_providers`
entry-point group.

## Configuration (`.qracer/`)

Config resolves in order (first found wins): `QRACER_CONFIG_DIR` → `./.qracer/` →
`~/.qracer/`. Files are deep-merged per file; credentials stay user-level.

```text
.qracer/
├── config.toml       — app settings (default_mode, llm_provider, loop tuning)
├── providers.toml    — data/LLM provider config (enabled, priority, tier, api_key_env, kind)
├── portfolio.toml    — holdings + risk limits
└── credentials.env   — API keys (user-level, gitignored)
```

Loader: `config/loader.py` (lazy singleton, mtime hot-reload). Models:
`config/models.py` (pydantic).

**Editing config** is schema-driven, so both front-ends stay in sync:
- `config/settings_schema.py` declares each editable setting once (`Setting` + the
  provider rows from `provider_settings()`). Add a setting here and it appears in
  both the CLI and the web form.
- `config/writer.py` is the single write path — `tomlkit` round-trip edits that
  preserve comments, formatting, and nested tables (`[briefing]`, `[providers.*]`,
  `[limits]`), plus a `credentials.env` upsert. Writes target `QRACER_CONFIG_DIR`
  if set, else `~/.qracer/`.
- Front-ends: `qracer config` (`get`/`set`/`providers` + a grouped listing) and the
  web dashboard **Settings** tab (`web/settings_ui.py`). API keys are masked and
  write-only. `qracer install` builds on the same writer.

Built-in adapters and external plugins share the same capability protocols
(`PriceProvider`, `NewsProvider`, …) and are registered from the same
`providers.toml` config.  Adapters may additionally implement the optional
`LifecycleProvider` protocol to opt into per-provider `initialize`,
`health_check`, and `shutdown` hooks — invoked by `_build_registries()` at
startup and by `qracer serve` on shutdown.

```python
# qracer/provider_lifecycle.py
@runtime_checkable
class LifecycleProvider(Protocol):
    async def initialize(self) -> None: ...
    async def health_check(self) -> bool: ...
    async def shutdown(self) -> None: ...
```

All three methods are optional — adapters without them are treated as
always-healthy and require no teardown.  A provider that raises in
`initialize()` or returns `False` from `health_check()` is excluded from the
registry with a warning instead of crashing the process.

### Example third-party adapter

```python
# qracer_polygon/adapter.py
class PolygonAdapter:
    """External data provider with graceful lifecycle management."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._session: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        self._session = httpx.AsyncClient(timeout=10.0)

    async def health_check(self) -> bool:
        if self._session is None:
            return False
        resp = await self._session.get("https://api.polygon.io/v1/status")
        return resp.status_code == 200

    async def shutdown(self) -> None:
        if self._session is not None:
            await self._session.aclose()

    async def get_price(self, ticker: str) -> float:
        ...
```

Register it via the entry-point group:

```toml
# External package pyproject.toml
[project.entry-points."qracer.data_providers"]
polygon = "qracer_polygon.adapter:PolygonAdapter"
```

### Built-in vs Plugin

| Type | Location | Install |
|------|----------|---------|
| Built-in | `qracer/data/adapters/` | Included in project |
| Plugin | External package | `uv add qracer-provider-*` |

### Plugin Discovery

External plugins register via Python entry points:

```toml
# External package pyproject.toml
[project.entry-points."qracer.providers"]
bloomberg = "qracer_bloomberg.adapter:BloombergAdapter"
```

On startup `_build_registries()` scans entry points, loads `providers.toml`,
checks credentials, runs each adapter's optional `initialize()` +
`health_check()` hooks, and registers the enabled/healthy providers.

```text
App start
  → entry_points("qracer.data_providers") + entry_points("qracer.llm_providers") scan
  → providers.toml config load (enabled, priority, api_key_env)
  → credentials.env check per provider
  → Missing API key → skip with warning log
  → Optional initialize() + health_check() → unhealthy ⇒ skip with warning
  → Register surviving providers by priority

qracer serve exit
  → shutdown_all_providers(data_registry, llm_registry)
  → Exceptions from shutdown() are logged, never propagated
```

### `providers.toml` Example

```toml
[providers.finnhub]
type = "builtin"
enabled = true
priority = 1
tier = "hot"
api_key_env = "FINNHUB_API_KEY"

[providers.bloomberg]
type = "plugin"
enabled = true
priority = 1
tier = "hot"
api_key_env = "BBG_API_KEY"

[providers.bloomberg.options]
terminal_host = "localhost"
terminal_port = 8194
```

## Real-Time Data

Real-time price (and news) streaming is provided by
`FinnhubWebSocketAdapter`, which implements the `StreamingProvider`
capability. It is enabled automatically by `qracer serve` when the
`finnhub` provider is enabled in `providers.toml` and the
`qracer[streaming]` extra is installed. Each trade message is
dispatched to `AlertMonitor.evaluate_price`, allowing threshold alerts
to trigger on the next tick instead of waiting for the next polling
interval.

For Live Mode, qracer needs sub-second price data and streaming news:

| Capability | Preferred Provider | Protocol | Fallback |
|---|---|---|---|
| Real-time quotes | Finnhub | WebSocket | REST polling (5s interval) |
| Streaming news | Finnhub | WebSocket | REST polling (30s interval) |
| Price/OHLCV | Finnhub | REST | yfinance |
| Fundamental | Finnhub | REST | FMP, yfinance |
| Macro | FRED | REST | World Bank |
| News/Sentiment | Finnhub | REST | NewsAPI, GDELT |
| Alternative | Finnhub | REST | SEC EDGAR |
| Earnings calendar | Finnhub | REST | FMP |
| Institutional holdings | SEC EDGAR | REST | FMP |
| Options flow (planned) | Unusual Whales | REST | Tradier (plugin) |
| Short interest (planned) | FINRA | REST | Ortex (plugin) |
| ETF flows (planned) | ETF.com | REST | — (plugin) |

WebSocket connections are opened when `qracer serve` starts and closed
on shutdown. If the initial handshake fails, the server transparently
falls back to REST polling via `AlertMonitor.check()`.

API key missing → adapter auto-skipped. Fallback kicks in transparently. Provider availability is controlled entirely by `providers.toml` — no code changes needed to toggle sources.

## Storage

DuckDB single-file database (`qracer.db`). Append-only for market data, analytical queries optimized.

```text
DuckDB (qracer.db)
├── prices             - OHLCV time series (daily append)
├── prices_intraday    - tick/1min data during live sessions (구현 예정)
├── fundamentals       - valuation, financial statements (quarterly append)
├── macro              - economic indicators (monthly append)
├── news               - articles + sentiment scores (daily append)
├── alternative        - insider trades, congressional trades, etc. (event append)
├── signals            - generated signal history
├── reports            - analysis report metadata
├── agent_logs         - agent execution logs
├── session_index      - session summary metadata + FTS index (구현 예정)
├── session_embeddings - session summary embeddings + HNSW index (구현 예정)
└── alerts             - active alert rules and trigger history (구현 예정)
```

Also serves as API cache to reduce rate limit pressure. Export to Parquet for backup/sharing.

## Storage & State

| Path | Backing | Written by |
|---|---|---|
| `~/.qracer/sessions/<id>.jsonl` | JSONL audit log | SessionLogger |
| `~/.qracer/summaries/<id>.md` | compacted summaries | SessionCompactor |
| `~/.qracer/memory_index.duckdb` | FTS + optional embeddings | MemorySearcher |
| `~/.qracer/fact_store.duckdb` | persisted theses | FactStore |
| `~/.qracer/{tasks,alerts,watchlist,agents}.json` | file stores | serve / repl / web |

See [memory-system.md](memory-system.md) for the session-memory tiers.

## Subsystems

| Area | Modules | Purpose |
|---|---|---|
| Conversation | `conversation/` | intent → handler → tools → synthesizer |
| Data | `data/` | capability protocols + adapters + registry |
| LLM | `llm/` | role routing + provider adapters (Claude/OpenAI/Gemini/OpenRouter) |
| Risk | `risk/` | position sizing, exposure, correlation, rebalance ([risk-system.md](risk-system.md)) |
| Memory | `memory/` | session logging, compaction, search, fact store |
| Tools | `tools/pipeline.py` | uniform `ToolResult`-returning tool wrappers |
| Daemon | `server.py`, `task_executor.py`, `alert_monitor.py`, `autonomous.py` | `qracer serve` ([serve.md](serve.md), [schedule.md](schedule.md)) |
| Custom agents | `agents_store.py`, `agent_runner.py`, `agent_monitor.py` | prompt-defined autonomous agents ([custom-agents.md](custom-agents.md)) |
| Web | `web/` | FastAPI API + NiceGUI dashboard (read-only status + editable Agents & Settings) |
| Notifications | `notifications/` | Telegram send + inbound poller |

## Roadmap — grounded investor harness

The plumbing above (protocol + registry + catalog seams, the risk math, the fact
store) is solid; the content layer is thin. The intended direction:

- **Investor profile + real position sizing** — a profile (risk tolerance, cash,
  goals, constraints) driving sizing unconditionally, not just when holdings exist.
- **Grounded, source-cited recommendations** — replace free-form LLM theses with a
  schema-enforced `Recommendation` (explicit action, sizing, entry/target/stop, each
  claim cited to a `ToolResult`) plus a verification step that reconciles the numbers
  against fetched data.
- **Pluggable data breadth** — new capabilities (events/earnings, estimates,
  benchmark, real sentiment, options) behind the existing registry seam, with 2+
  adapters per capability so fallback is real.
- **Feedback loop** — thesis lifecycle (hit/stopped/invalidated) tracked to build a
  hit-rate that tempers conviction.
- **Intentional role→model routing** — today the role is not passed into
  `complete()`, so per-role model tiering never engages; this needs fixing or
  collapsing.
