# Agent Pipeline (Tracer Cycle)

```text
Screening → Macro Regime → Cross-Market Discovery → Consensus Mapping
    → Contrarian Detection → Conviction Scoring → Alpha Report
```

## Step 1: Universe Screening
- Filter global markets by region, sector, market cap, liquidity
- Narrow down analysis targets from thousands to actionable set
- Agent: **researcher** | Data: PriceProvider, FundamentalProvider

## Step 2: Macro Regime Detection
- Determine current market regime: risk-on / risk-off / transition
- Analyze interest rates, inflation, GDP trends, currency movements
- Regime determines which strategies and sectors to prioritize
- Agent: **analyst** | Data: MacroProvider

## Step 3: Cross-Market Discovery (core alpha)
- Find information asymmetry across global markets
- Detect leading indicators in one market that predict another
- Example: Korea semiconductor exports → US AI stock forward indicator
- Example: China property regulation → commodity demand → AUD weakness → BHP earnings
- Agent: **analyst** | Data: PriceProvider, MacroProvider, NewsProvider, AlternativeProvider

## Step 4: Consensus Mapping
- Collect what the market currently believes
- Analyst ratings, news sentiment, institutional positioning (13F), insider trades
- Build a "consensus view" for each target
- Agent: **researcher** | Data: NewsProvider, AlternativeProvider, FundamentalProvider

## Step 5: Contrarian Detection (core alpha)
- Compare Step 3 findings against Step 4 consensus
- Find where consensus is wrong, late, or ignoring signals
- Identify: oversold with improving fundamentals, overhyped with deteriorating data, ignored catalysts
- Agent: **strategist** | Data: all providers

## Step 6: Conviction Scoring
- Score each signal by strength, time horizon, and risk
- Factors: data quality, signal convergence, historical hit rate, downside scenario
- Output: ranked list of high-conviction ideas with risk assessment
- Agent: **strategist**

## Step 7: Alpha Report
- Generate actionable investment report
- Contents: thesis, supporting evidence, contrarian angle, risk factors, timeline, position sizing suggestion
- Format: "What the market doesn't know yet" narrative
- Agent: **reporter**
