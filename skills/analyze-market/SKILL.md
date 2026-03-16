---
name: analyze-market
description: >
  Run the full Tracer Cycle: screen global universe, detect macro regime,
  discover cross-market alpha, map consensus, detect contrarian signals,
  score conviction, and generate an alpha report. Use when performing
  end-to-end investment analysis on a market, sector, or theme.
---

# Analyze Market

Execute the 7-step Tracer Cycle for a given market, sector, or investment theme.

## Before You Start

- Read `AGENTS.md` for architecture and provider setup.
- Ensure API keys are configured in `.env` (FINNHUB_API_KEY, FRED_API_KEY, etc.).
- Confirm `uv sync` has been run.

## Input

Ask the user for:
- **Target**: market/sector/theme/ticker (e.g., "US AI semiconductors", "global energy transition", "AAPL vs TSMC")
- **Time horizon**: short-term (1-4 weeks), medium (1-6 months), long (6-24 months)
- **Focus**: alpha discovery, contrarian signal, or both

## Tracer Cycle Execution

### Step 1: Universe Screening
- Identify relevant tickers across global markets
- Filter by market cap, liquidity, sector relevance
- Output: list of 10-30 tickers to analyze
- Data: PriceProvider, FundamentalProvider

### Step 2: Macro Regime Detection
- Pull macro indicators: interest rates, CPI, GDP, PMI, currency pairs
- Classify regime: risk-on / risk-off / transition
- Identify which sectors/regions benefit in current regime
- Data: MacroProvider (FRED primary)

### Step 3: Cross-Market Discovery
- Find information asymmetry across markets
- Look for leading indicators in one market predicting another
- Check for divergences: price vs fundamentals, sector A vs sector B, region X vs region Y
- Patterns to seek:
  - Supply chain linkage (upstream event → downstream impact)
  - Currency/commodity correlation breaks
  - Regulatory spillover across jurisdictions
  - Earnings revision momentum vs price action
- Data: PriceProvider, MacroProvider, NewsProvider, AlternativeProvider

### Step 4: Consensus Mapping
- Gather analyst ratings and price targets
- Measure news sentiment (bullish/bearish/neutral distribution)
- Check institutional positioning: 13F filings, insider trades
- Build consensus score per ticker: -1 (extreme bearish) to +1 (extreme bullish)
- Data: NewsProvider, AlternativeProvider, FundamentalProvider

### Step 5: Contrarian Detection
- Compare Step 3 findings against Step 4 consensus
- Flag signals where:
  - Consensus is bullish but cross-market data shows deterioration
  - Consensus is bearish but leading indicators are improving
  - Market is ignoring a catalyst (regulatory, earnings, macro shift)
  - Positioning is extremely one-sided (crowded trade risk)
- Output: contrarian signal candidates with reasoning

### Step 6: Conviction Scoring
- Score each signal (1-10) based on:
  - Data quality and recency
  - Number of confirming cross-market signals
  - Historical pattern reliability
  - Magnitude of consensus-reality gap
  - Risk/reward asymmetry
- Filter: only pass signals with conviction >= 7

### Step 7: Alpha Report
- Invoke `/alpha-report` skill for final output
- Include: thesis, evidence chain, contrarian angle, risks, timeline

## Output Format

```
=== TRACER ANALYSIS: {target} ===
Date: {date}
Horizon: {horizon}
Regime: {regime classification}

--- HIGH CONVICTION SIGNALS ---
1. [Signal Name] (Conviction: X/10)
   Thesis: ...
   Contrarian Angle: ...
   Key Evidence: ...
   Risk: ...
   Timeline: ...

--- CROSS-MARKET INSIGHTS ---
- {insight 1}
- {insight 2}

--- CONSENSUS VS REALITY ---
| Ticker | Consensus | Tracer View | Gap |
|--------|-----------|-------------|-----|
| ...    | ...       | ...         | ... |
```

## Notes

- Always cite data sources and timestamps.
- Flag when data is stale or incomplete.
- Never present signals as guaranteed outcomes. Include uncertainty.
- Rate limit awareness: batch API calls, use caching for repeated lookups.
