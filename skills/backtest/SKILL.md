---
name: backtest
description: >
  Backtest a Tracer signal or strategy against historical data. Use to validate
  whether a cross-market or contrarian signal would have generated alpha in the past.
  Helps calibrate conviction scoring and identify false positive patterns.
---

# Backtest

Validate signals and strategies against historical data.

## Before You Start

- Ensure historical data is available via PriceProvider (yfinance preferred for backfill).
- Define the signal clearly before backtesting. Vague signals produce meaningless results.

## Input

Ask the user for:
- **Signal/Strategy**: what triggers a buy/sell (e.g., "buy when Korea semi exports rise 10% MoM and US AI stocks haven't moved")
- **Universe**: which tickers/markets to test
- **Period**: backtest date range (e.g., 2020-01 to 2025-12)
- **Benchmark**: what to compare against (e.g., S&P 500, sector ETF)

## Backtest Workflow

### Step 1: Define Signal Rules
- Entry condition: exact trigger criteria
- Exit condition: take profit, stop loss, time-based exit, or signal reversal
- Position sizing: equal weight, conviction-weighted, or risk-parity

### Step 2: Gather Historical Data
- Pull OHLCV data for universe + benchmark
- Pull any additional data the signal requires (macro, sentiment, fundamentals)
- Handle: missing data, delistings, survivorship bias
- Data: PriceProvider (yfinance for historical depth)

### Step 3: Run Backtest
- Walk-forward: no future data leakage
- Transaction costs: include realistic slippage and commission estimates
- Rebalance frequency: match the signal's natural frequency

### Step 4: Calculate Metrics
Report these metrics at minimum:

| Metric | Description |
|--------|-------------|
| Total Return | Cumulative return over period |
| CAGR | Compound annual growth rate |
| Sharpe Ratio | Risk-adjusted return (target: > 1.0) |
| Max Drawdown | Worst peak-to-trough decline |
| Win Rate | % of trades that were profitable |
| Profit Factor | Gross profit / gross loss |
| Alpha | Excess return vs benchmark |
| Beta | Correlation to benchmark |
| # of Trades | Total trade count (too few = unreliable) |

### Step 5: Analyze Results
- Is alpha statistically significant or could it be luck?
- Minimum 30 trades for any reliability.
- Check for regime dependency: does it only work in bull markets?
- Look for clustering: are wins/losses concentrated in specific periods?
- Compare in-sample vs out-of-sample if possible.

## Output Format

```
=== BACKTEST: {strategy name} ===
Period: {start} to {end}
Universe: {tickers}
Benchmark: {benchmark}

--- PERFORMANCE ---
Total Return:   {x}%  (Benchmark: {y}%)
CAGR:           {x}%
Sharpe Ratio:   {x}
Max Drawdown:   {x}%
Win Rate:       {x}%
Profit Factor:  {x}
Alpha:          {x}%
Beta:           {x}
Total Trades:   {n}

--- REGIME ANALYSIS ---
Bull markets:  {return}%
Bear markets:  {return}%
Sideways:      {return}%

--- VERDICT ---
{Reliable / Promising but needs more data / Unreliable / Overfitted}
Confidence: {low/medium/high}
Recommendation: {use as-is / refine signal / discard}
```

## Common Pitfalls

- **Survivorship bias**: only testing stocks that still exist today.
- **Look-ahead bias**: using data that wasn't available at signal time.
- **Overfitting**: too many parameters tuned to historical data.
- **Small sample**: fewer than 30 trades means results are noise.
- **Ignoring transaction costs**: alpha disappears with realistic costs.

## Notes

- Backtests prove a signal *could have* worked, not that it *will* work.
- Always out-of-sample test: split data into train/test periods.
- Regime changes can invalidate historical patterns entirely.
