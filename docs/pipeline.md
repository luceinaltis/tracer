# Pipeline

How a natural-language query becomes a response. Everything runs through
`ConversationEngine.query()` (`conversation/engine.py`), which parses intent and
routes to one of four handlers.

```text
query
  → extract context (last N turns)
  → IntentParser.parse → Intent(type, tickers, tools, raw_query)
  → route by intent type:
        PORTFOLIO_CHECK        → PortfolioHandler
        quick intents          → QuickPathHandler
        COMPARISON (≥2 tickers) → ComparisonHandler
        else                   → StandardHandler
  → persist turn, maybe compact, persist thesis to FactStore
```

## Handlers

### QuickPathHandler — template, no LLM
Fast lookups (price, quick news). Fetches 1–2 tools and formats a template response.
Target: sub-second. No LLM call.

### PortfolioHandler — deterministic
Prices → portfolio snapshot (`RiskCalculator`) → limit check → optional rebalance
suggestion. No LLM narrative; pure math over `portfolio.toml` holdings.

### ComparisonHandler — per-ticker fan-out
Builds one sub-intent per ticker, runs `invoke_tools` for each concurrently
(`asyncio.gather`), then `ComparisonSynthesizer` renders a side-by-side table +
verdict.

### StandardHandler — the deep path
The full evidence → analysis → thesis → risk → synthesis flow:

1. **Gather evidence** — `invoke_tools(intent.tools, …)` runs the intent's tools
   concurrently, each returning a uniform `ToolResult`.
2. **Prior theses** — open theses for the tickers are pulled from `FactStore` and
   injected as additional evidence.
3. **Analysis loop** — `AnalysisLoop.run(...)` (below) gathers more data until it is
   confident enough or hits the iteration cap.
4. **Trade thesis** — `pipeline.trade_thesis(...)` (STRATEGIST role) produces
   `entry_zone`, `target`, `stop`, `risk_reward`, `catalyst`, `conviction` (1–10).
5. **Risk check** — *only when a thesis exists and `portfolio.toml` has holdings*,
   `pipeline.risk_check(...)` sizes the position via `RiskCalculator`
   (see [risk-system.md](risk-system.md)).
6. **Synthesize** — `ResponseSynthesizer` (STRATEGIST role) renders the final
   report over the successful `ToolResult`s.

## AnalysisLoop

`conversation/analysis_loop.py`. Iterative data-sufficiency loop:

- Up to `MAX_ITERATIONS = 3`; exits when `confidence >= CONFIDENCE_THRESHOLD (0.7)`,
  when no more tools are missing, or on the last iteration.
- Bails early if ≥2 tools failed on iteration 0, or after 2 consecutive eval failures.
- Each iteration asks the **ANALYST** role for `{confidence, missing_tools}`, then
  fetches any missing tools and loops.

> `confidence` here is the LLM's self-rating of *data sufficiency* — it gates
> data-gathering only. It is **not** the thesis `conviction`, and neither number is
> statistically calibrated.

## Tools

`tools/pipeline.py` — thin async wrappers returning `ToolResult`. Selected by intent
via `INTENT_TOOL_MAP` (`intent.py`) and dispatched by `dispatcher.py`.

| Tool | Source | Notes |
|---|---|---|
| `price_event` | PriceProvider | current price + OHLCV |
| `news` | NewsProvider | 30-day window; sentiment not populated |
| `insider` | AlternativeProvider | 90-day Finnhub insider transactions |
| `fundamentals` | FundamentalProvider | PE, market cap, revenue, earnings, div yield |
| `macro` | MacroProvider | FRED series |
| `cross_market` | PriceProvider | fetches prices per ticker (no correlation computed) |
| `trade_thesis` | LLM (STRATEGIST) | entry/target/stop/conviction — LLM-generated |
| `risk_check` | RiskCalculator | sizing over live prices + portfolio |
| `memory_search` | MemorySearcher | past-session retrieval |

## Error handling

| Failure | Behavior |
|---|---|
| Single tool fails | excluded from evidence; noted as unavailable |
| ≥2 tools fail on iteration 0 | analysis loop exits early with caveat |
| LLM JSON parse fails | fall back to a safe default, warn |
| No ticker resolvable | ask the user rather than guess |

## Known gaps

- No explicit **direction** field — the thesis assumes a long trade; sell/hold/avoid
  are not produced.
- `trade_thesis` numbers are LLM guesses with only an arithmetic risk/reward check —
  there is no verification that they match the fetched data.
- `risk_check` (and therefore sizing) is skipped entirely when no holdings are
  configured.

These are the focus of the investor-harness roadmap in
[architecture.md](architecture.md).
