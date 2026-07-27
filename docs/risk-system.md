# Risk System

Portfolio-aware risk math in `qracer/risk/`. It backs the `risk_check` tool
(`tools/pipeline.py`) in the deep path (see [pipeline.md](pipeline.md)) and the
`PortfolioHandler`. This is the most substantive "real math" in the repo —
`calculator.py` (snapshot, exposure, sizing, rebalance) and `correlation.py`.

## Portfolio Model

Holdings are loaded from `.qracer/portfolio.toml`:

```toml
[portfolio]
currency = "USD"

[[portfolio.holdings]]
ticker = "AAPL"
shares = 100
avg_cost = 165.00

[[portfolio.holdings]]
ticker = "TSMC"
shares = 200
avg_cost = 140.00

[portfolio.limits]
max_single_position_pct = 15    # max % of portfolio in one name
max_sector_pct = 40             # max % in one sector
max_drawdown_alert_pct = 10     # alert when portfolio drawdown exceeds this
```

P&L is computed from live prices at query time. Sector classification comes from a
`SectorResolver` (provider industry with a small hardcoded fallback map).

## Exposure Breakdown

The risk module maintains a live view of portfolio exposure:

| Dimension | Calculation | Source |
|-----------|------------|--------|
| Sector concentration | Market value per GICS sector / total | FundamentalProvider |
| Geography exposure | Revenue-weighted country allocation | FundamentalProvider |
| Beta | Portfolio-weighted beta vs benchmark | PriceProvider (90-day) |
| Correlation matrix | Pairwise correlation between holdings | PriceProvider (90-day) |

## Risk Metrics

| Metric | Description |
|--------|-------------|
| Portfolio beta | Weighted average beta vs S&P 500 |
| Sharpe ratio | Risk-adjusted return (rolling 90-day) |
| Max drawdown | Largest peak-to-trough decline |
| Current drawdown | Current level vs all-time high |
| Sector concentration | Largest sector weight |
| Correlation risk | Average pairwise correlation (high = clustered risk) |

## Position Sizing

Conviction score (1-10) from the trade thesis maps to a base allocation:

| Conviction | Base Allocation | Description |
|-----------|----------------|-------------|
| 8-10 | 3-5% of portfolio | High conviction |
| 5-7 | 1-3% of portfolio | Moderate conviction |
| 1-4 | 0.5-1% of portfolio | Low conviction / tracking position |

Base allocation is then adjusted by:

1. **Sector exposure** — reduce if sector already near limit
2. **Correlation** — reduce if highly correlated with existing large positions
3. **Volatility** — reduce for high-vol names to normalize risk contribution
4. **Hard limits** — never exceed `max_single_position_pct` from `portfolio.toml`

## Integration with the pipeline

`risk_check` runs in `StandardHandler` after a trade thesis is produced:

```text
Trade thesis (conviction, entry/target/stop)
    → load portfolio snapshot from portfolio.toml
    → size_position(conviction) with sector/correlation/vol haircuts
    → enforce hard limits
    → sized recommendation string ("Allocate X% to TICKER")
```

## Known limitations (roadmap)

The math is real but narrow — the investor-harness roadmap
([architecture.md](architecture.md)) addresses these:

- **Sizing is gated on holdings.** `risk_check` only runs when `portfolio.toml` has
  holdings, so a new user with no positions gets a thesis with **no size**. Sizing
  should be unconditional (size against total investable capital).
- **No investor profile.** Sizing keys off a bare `conviction` integer — there is no
  risk tolerance, cash balance, goals, or constraints input.
- **No Kelly / vol-targeting / risk-parity.** Sizing is a hand-tuned conviction
  ladder. Portfolio `beta` is computed but not used for sizing.
- **Limit check is post-hoc**, not pre-trade; drawdown peak is in-memory (resets per
  process).
