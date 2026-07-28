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
