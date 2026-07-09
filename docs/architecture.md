# Architecture

## Dual-Mode Design

qracer operates in two modes based on context:

| | Live Mode | Research Mode |
|---|---|---|
| **When** | Market hours, quick queries | On-demand, explicit request |
| **Latency** | < 5 seconds | 30–120 seconds |
| **LLM calls** | 0–1 | 3–7 |
| **Data** | Real-time quotes, cached fundamentals | Full fetch across all providers |
| **Output** | Short answer, table, or alert | Full analysis report with evidence chain |

Mode is selected automatically by the IntentRouter based on query complexity, but can be forced by the user (`/deep`, `/quick`).

## QuickPath vs DeepPath

```text
QuickPath (Live Mode)
  User query → IntentRouter → 1-2 data tools → format response
  Target: < 5s end-to-end

DeepPath (Research Mode)
  User query → IntentRouter → ResearchPipeline (9-step) → full report
  Target: async, notify on completion
```

QuickPath handles ~80% of market-hours queries: price checks, news summaries, quick comparisons, follow-ups. DeepPath is triggered explicitly or when the query requires cross-market analysis.

## Adapter + Capability Registry Pattern

Adapters register capabilities, registry routes requests by capability. Agents never reference a specific source directly.

```python
class FinnhubAdapter:
    capabilities = [Price, News, Insider, Congress]

    async def get_price(self, ticker: str) -> float: ...
    async def get_news(self, ticker: str) -> list[News]: ...

# Agents request by capability, not by source
registry.get(Price)          # → FinnhubAdapter (primary)
registry.get(Price, "yf")    # → YfinanceAdapter (explicit)
```

### Latency Constraints

Adapters used in QuickPath must meet latency requirements:

| Tier | Max Latency | Used In | Examples |
|------|-------------|---------|----------|
| **hot** | < 2s | QuickPath | Price, cached News |
| **warm** | < 10s | Either | Fundamentals, Macro |
| **cold** | unbounded | DeepPath only | Full backfill, alternative data |

Registry tags each adapter with its tier. QuickPath only dispatches to hot/warm adapters. If a hot adapter times out, the response includes a staleness caveat rather than blocking.

## Config Directory (`.qracer/`)

User and project settings live in a `.qracer/` directory. Resolution order (first found wins):

1. `--config-dir` CLI flag
2. `QRACER_CONFIG_DIR` environment variable
3. `./.qracer/` — project-local, team-shareable via git
4. `~/.qracer/` — user default

Project-local and user configs merge per file: `./.qracer/providers.toml` defines shared provider list, `~/.qracer/credentials.env` supplies personal API keys. Credentials always stay user-level, never committed.

```text
.qracer/
├── config.toml        - global settings (default mode, LLM preferences)
├── providers.toml     - data source config (enabled, priority, tier, api_key_env)
├── portfolio.toml     - watchlist, holdings, risk limits
└── credentials.env    - API keys (user-level only, gitignored)
```

## Provider Plugin System

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

## Rate Limits

| Source | Free Tier Limit | Notes |
|---|---|---|
| Finnhub | 60 req/min | WebSocket preferred for real-time |
| yfinance | ~2000 req/hr (est) | Historical backfill only, avoid real-time |
| FRED | 120 req/min | Generous |
| FMP | 250 req/day | Low; fallback only |
