# Architecture

## Adapter + Capability Registry Pattern

Both LLM and Data layers use the same pattern: adapters register capabilities,
registry routes requests by capability. Agents never reference a specific source directly.

```text
Agent: "I need Price data for AAPL"
  → Registry.get(Price)
  → Returns FinnhubAdapter (primary) or YfinanceAdapter (fallback)
```

## LLM Layer

Each adapter registers its capabilities. Prototype defaults to Claude for all roles.
Per-role assignment is overridable via config. See Project Structure in AGENTS.md for directory layout.

## Data Layer

Each adapter is a single class with one client, registering multiple capabilities.
Registry auto-routes by capability with fallback:

```python
class FinnhubAdapter:
    capabilities = [Price, News, Insider, Congress]

    def __init__(self, api_key: str):
        self.client = FinnhubClient(api_key)

    async def get_price(self, ticker: str) -> float: ...
    async def get_news(self, ticker: str) -> list[News]: ...

# Agents request by capability, not by source
registry.get(Price)          # → FinnhubAdapter (primary)
registry.get(Price, "yf")    # → YfinanceAdapter (explicit)
```

Default capability routing (overridable via config):

| Capability    | Primary    | Fallback       |
|---------------|------------|----------------|
| Price/OHLCV   | Finnhub    | yfinance       |
| Fundamental   | Finnhub    | FMP, yfinance  |
| Macro         | FRED       | World Bank     |
| News/Sentiment| Finnhub    | NewsAPI, GDELT |
| Alternative   | Finnhub    | SEC EDGAR      |

API key missing → adapter auto-skipped. Fallback kicks in transparently.

## Storage

DuckDB single-file database (`tracer.db`). Append-only for market data, analytical queries optimized.

```text
DuckDB (tracer.db)
├── prices             - OHLCV time series (daily append)
├── fundamentals       - valuation, financial statements (quarterly append)
├── macro              - economic indicators (monthly append)
├── news               - articles + sentiment scores (daily append)
├── alternative        - insider trades, congressional trades, etc. (event append)
├── signals            - generated signal history
├── reports            - analysis report metadata
├── agent_logs         - agent execution logs
├── session_index      - session summary metadata + FTS index (keyword search)
└── session_embeddings - session summary embeddings + HNSW index (vector search)
```

Also serves as API cache to reduce rate limit pressure.
Export to Parquet for backup/sharing.

## Rate Limits (reference)

| Source     | Free Tier Limit     | Notes                          |
|------------|--------------------|---------------------------------|
| Finnhub    | 60 req/min         | Stable, well-documented         |
| yfinance   | ~2000 req/hr (est) | Unofficial, can get IP-blocked  |
| FRED       | 120 req/min        | Generous                        |
| FMP        | 250 req/day        | Low; use as fallback            |

yfinance: use only for historical data backfill. Avoid repeated real-time calls.
