---
name: add-data-provider
description: >
  Guide for implementing a new data provider. Use when adding a new market data
  source (e.g., new API, new data type) to the Tracer system. Ensures the provider
  follows the abstraction interface and integrates cleanly.
---

# Add Data Provider

Step-by-step guide for adding a new data source to Tracer.

## Before You Start

- Read `docs/architecture.md` for provider abstraction design.
- Read `src/tracer/data/base.py` for existing interfaces (once implemented).
- Identify which interface(s) the new provider implements:
  - `PriceProvider` - stock price, OHLCV, historical
  - `FundamentalProvider` - financial statements, valuation metrics
  - `MacroProvider` - economic indicators, rates, currencies
  - `NewsProvider` - news articles, sentiment
  - `AlternativeProvider` - insider trades, congressional trades, SEC filings, ESG

## Checklist

1. **Research the API**
   - Read official docs. Check: auth method, rate limits, data format, coverage.
   - Verify free tier is sufficient for prototype.
   - Document rate limits in `AGENTS.md` rate limits table.

2. **Create provider file**
   - Path: `src/tracer/data/{provider_name}.py`
   - Implement the relevant interface(s) from `base.py`
   - All API calls must be `async`
   - Handle rate limiting: implement backoff/retry
   - Handle errors: raise typed exceptions, never swallow silently

3. **Configuration**
   - API key via environment variable: `{PROVIDER_NAME}_API_KEY`
   - Add to `.env.example`
   - Register in provider config/registry

4. **Write tests**
   - Path: `tests/data/test_{provider_name}.py`
   - Unit tests with mocked API responses (no real API calls in CI)
   - At least: one success case, one error case, one rate limit case per method

5. **Update docs**
   - Add provider to `docs/architecture.md` capability routing table
   - Add rate limit info to `docs/architecture.md` rate limits table

## Implementation Template

```python
from tracer.data.base import PriceProvider  # or relevant interface
from tracer.models import OHLCV  # or relevant model


class MyNewProvider(PriceProvider):
    """Provider for MyNewAPI data source."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def get_price(self, ticker: str) -> float:
        ...

    async def get_ohlcv(self, ticker: str, start: str, end: str) -> list[OHLCV]:
        ...
```

## Validation

- [ ] Implements interface completely (no `NotImplementedError` left)
- [ ] All methods are async
- [ ] Rate limiting handled
- [ ] Error handling with typed exceptions
- [ ] Tests pass: `pytest tests/data/test_{provider_name}.py`
- [ ] Type check passes: `pyright src/tracer/data/{provider_name}.py`
- [ ] `docs/architecture.md` updated
