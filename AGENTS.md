# AGENTS.md

## Project Overview

Tracer - Investing agent for discovering alpha in global security markets.
Core capabilities: cross-market alpha discovery, contrarian signal detection.

## Design Philosophy

### 1. Data-First, Opinion-Later
에이전트는 데이터를 먼저 확보하고, 그 위에서 판단한다.
감이나 추론으로 시작하지 않는다. 항상 근거가 먼저다.

### 2. Autonomous Reasoning Loop
고정된 파이프라인을 따르되, 각 단계에서 에이전트가 스스로 판단한다:
- "이 데이터만으로 충분한가?"
- "확신이 부족하면 어떤 데이터가 더 필요한가?"
- "다른 소스를 추가로 조회해야 하는가?"

에이전트는 확신이 충분해질 때까지 데이터를 추가 수집하고 재분석한다.
한 방향으로 결론 내리고 끝내는 것이 아니라, 반박할 수 있는 데이터도 스스로 찾는다.

탈출 조건: 최대 3회 반복 또는 비용 한도 도달 시 현재까지의 결과로 진행한다.

```
┌→ Collect Data
│     ↓
│   Analyze
│     ↓
│   Evaluate Confidence ──→ "충분하다" → Next Step
│     │                 ──→ "최대 반복/비용 한도" → Next Step (with caveat)
│     │
│     └→ "부족하다" → 어떤 데이터가 필요한지 판단
│           ↓
│         추가 데이터 소스 호출
│           ↓
└─────── 재분석
```

### 3. Self-Directed Data Acquisition
에이전트가 분석 중 필요한 데이터를 스스로 결정하고 가져온다.
미리 정해진 데이터 세트만 보는 것이 아니라, 분석 맥락에 따라
동적으로 데이터 소스를 선택한다.

예시:
- 반도체 분석 중 → "공급망 확인 필요" → 대만 수출 데이터 추가 조회
- 컨센서스 분석 중 → "내부자 거래 확인 필요" → Alternative 데이터 추가 조회
- 매크로 분석 중 → "환율 영향 확인 필요" → 통화쌍 데이터 추가 조회

### 4. No Hallucination (데이터 무결성)
금융 데이터를 지어내지 않는다. 이것은 가장 중요한 원칙이다.
- 데이터가 없으면 "없다"고 명시한다. 추정으로 채우지 않는다.
- API 응답이 실패하면 에이전트에게 실패 사실을 그대로 전달한다.
- 출처 없는 수치는 리포트에 포함하지 않는다.

### 5. Adversarial Self-Check
결론을 내리기 전에 스스로 반박한다.
- "이 시그널이 틀릴 수 있는 이유는?"
- "반대 포지션의 근거는 무엇인가?"
- 반박에 견딜 수 있을 때만 시그널로 채택한다.

### 6. Calibrated Conviction (보정된 확신)
시그널을 "있다/없다"로 이분하지 않는다. 확신도와 함께 제시한다.
- Conviction 8-10: High conviction signal → 리포트에 포함
- Conviction 5-7: Developing signal → 워치리스트, 추가 모니터링
- Conviction 1-4: Weak signal → 기록만, 데이터 쌓이면 재평가
약한 시그널도 버리지 않는다. 시간이 지나며 강해질 수 있다.

### 7. Cost-Aware Execution
데이터 수집에는 비용(API 호출, rate limit, 시간)이 든다.
- 루프를 돌 때마다 "이 추가 조회의 가치 > 비용인가?"를 판단한다.
- 캐시를 적극 활용하여 동일 데이터 재조회를 방지한다.
- rate limit에 가까워지면 우선순위가 높은 데이터부터 수집한다.

### 8. Traceable Reasoning
모든 판단에는 추적 가능한 근거 체인이 있어야 한다.
- 어떤 데이터를 봤고 → 어떤 분석을 했고 → 왜 이 결론에 도달했는지
- 사후에 "왜 이런 판단을 했는가?"에 답할 수 있어야 한다.

## Tech Stack

- Language: Python 3.12+
- Package manager: uv
- Linter/Formatter: ruff
- Type checker: pyright
- Test: pytest
- LLM: multi-provider (Claude, OpenAI, Gemini, etc.)
- Data: multi-source (Finnhub, yfinance, FRED, FMP, etc.)

## Architecture

### Adapter + Capability Registry Pattern

Both LLM and Data layers use the same pattern: adapters register capabilities,
registry routes requests by capability. Agents never reference a specific source directly.

```
Agent: "I need Price data for AAPL"
  → Registry.get(Price)
  → Returns FinnhubAdapter (primary) or YfinanceAdapter (fallback)
```

### LLM Layer

```
llm/
├── base.py            # LLMProvider protocol (Chat, StructuredOutput, Streaming)
├── registry.py        # role → adapter routing, fallback chain
└── adapters/
    ├── claude.py      # ClaudeAdapter
    ├── openai.py      # OpenAIAdapter
    └── gemini.py      # GeminiAdapter
```

Each adapter registers its capabilities. Prototype defaults to Claude for all roles.
Expand per-role assignment via config when needed.

Roles and default assignments (overridable via config):

| Role       | Description                          | Default          |
|------------|--------------------------------------|------------------|
| researcher | Gather and summarize market data     | Claude Sonnet    |
| analyst    | Deep financial/cross-market analysis | Claude Opus      |
| strategist | Investment decision and signal gen   | Claude Opus      |
| reporter   | Summary and report generation        | Claude Haiku     |

### Data Layer

```
data/
├── base.py            # Capability protocols (Price, Fundamental, Macro, News, Alternative)
├── registry.py        # capability → adapter routing, fallback chain
└── adapters/
    ├── finnhub.py     # Price, News, Alternative (insider, congress)
    ├── yfinance.py    # Price, Fundamental
    ├── fred.py        # Macro
    └── fmp.py         # Fundamental, Alternative (SEC filings)
```

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

### Storage

DuckDB single-file database (`tracer.db`). Append-only for market data, analytical queries optimized.

```
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

### Rate Limits (reference)

| Source     | Free Tier Limit     | Notes                          |
|------------|--------------------|---------------------------------|
| Finnhub    | 60 req/min         | Stable, well-documented         |
| yfinance   | ~2000 req/hr (est) | Unofficial, can get IP-blocked  |
| FRED       | 120 req/min        | Generous                        |
| FMP        | 250 req/day        | Low; use as fallback            |

yfinance: use only for historical data backfill. Avoid repeated real-time calls.

## Project Structure

```
tracer/
├── AGENTS.md
├── pyproject.toml
├── src/
│   └── tracer/
│       ├── __init__.py
│       ├── agents/            # Agent roles (researcher, analyst, strategist, reporter)
│       ├── conversation/      # Conversational layer
│       │   ├── engine.py      # ConversationEngine — session context, turn history
│       │   ├── intent.py      # IntentParser — query → Intent dataclass
│       │   ├── synthesizer.py # ResponseSynthesizer — causal chain + adversarial check
│       │   └── session.py     # SessionManager — JSONL append, compaction
│       ├── memory/            # Session memory search
│       │   ├── search.py      # memory_search tool — hybrid FTS + HNSW via DuckDB
│       │   └── index.py       # DuckDB session index — embedding generation, upsert
│       ├── tools/             # Pipeline tool wrappers
│       │   ├── price_event.py
│       │   ├── news.py
│       │   ├── insider.py
│       │   ├── macro.py
│       │   ├── fundamentals.py
│       │   └── cross_market.py
│       ├── llm/               # LLM adapter + capability registry
│       │   ├── base.py        # LLMProvider protocol
│       │   ├── registry.py    # Role → adapter routing
│       │   └── adapters/
│       │       ├── claude.py
│       │       ├── openai.py
│       │       └── gemini.py
│       ├── data/              # Data adapter + capability registry
│       │   ├── base.py        # Capability protocols (Price, Fundamental, etc.)
│       │   ├── registry.py    # Capability → adapter routing, fallback
│       │   └── adapters/
│       │       ├── finnhub.py   # Price, News, Alternative
│       │       ├── yfinance.py  # Price, Fundamental
│       │       ├── fred.py      # Macro
│       │       └── fmp.py       # Fundamental, Alternative
│       ├── models/            # Domain models (Stock, Signal, Report, etc.)
│       ├── storage/           # DuckDB persistence layer
│       │   ├── db.py          # Connection management
│       │   └── tables.py      # Schema definitions
│       └── config/            # Configuration loader
├── tests/
│   ├── agents/
│   ├── conversation/
│   ├── llm/
│   ├── data/
│   ├── storage/
│   └── strategies/
├── skills/                    # Agent skill definitions (SKILL.md per skill)
└── scripts/                   # CLI entry points, one-off utilities
```

## Agent Pipeline (Tracer Cycle)

```
Screening → Macro Regime → Cross-Market Discovery → Consensus Mapping
    → Contrarian Detection → Conviction Scoring → Alpha Report
```

### Step 1: Universe Screening
- Filter global markets by region, sector, market cap, liquidity
- Narrow down analysis targets from thousands to actionable set
- Agent: **researcher** | Data: PriceProvider, FundamentalProvider

### Step 2: Macro Regime Detection
- Determine current market regime: risk-on / risk-off / transition
- Analyze interest rates, inflation, GDP trends, currency movements
- Regime determines which strategies and sectors to prioritize
- Agent: **analyst** | Data: MacroProvider

### Step 3: Cross-Market Discovery (core alpha)
- Find information asymmetry across global markets
- Detect leading indicators in one market that predict another
- Example: Korea semiconductor exports → US AI stock forward indicator
- Example: China property regulation → commodity demand → AUD weakness → BHP earnings
- Agent: **analyst** | Data: PriceProvider, MacroProvider, NewsProvider, AlternativeProvider

### Step 4: Consensus Mapping
- Collect what the market currently believes
- Analyst ratings, news sentiment, institutional positioning (13F), insider trades
- Build a "consensus view" for each target
- Agent: **researcher** | Data: NewsProvider, AlternativeProvider, FundamentalProvider

### Step 5: Contrarian Detection (core alpha)
- Compare Step 3 findings against Step 4 consensus
- Find where consensus is wrong, late, or ignoring signals
- Identify: oversold with improving fundamentals, overhyped with deteriorating data, ignored catalysts
- Agent: **strategist** | Data: all providers

### Step 6: Conviction Scoring
- Score each signal by strength, time horizon, and risk
- Factors: data quality, signal convergence, historical hit rate, downside scenario
- Output: ranked list of high-conviction ideas with risk assessment
- Agent: **strategist**

### Step 7: Alpha Report
- Generate actionable investment report
- Contents: thesis, supporting evidence, contrarian angle, risk factors, timeline, position sizing suggestion
- Format: "What the market doesn't know yet" narrative
- Agent: **reporter**

## Conversational Layer

Users interact with Tracer via natural language queries. The conversational layer wraps the Tracer Cycle steps as discrete tools, manages session context, and routes each query to the appropriate pipeline steps.

```
CLI (REPL)
    ↓
ConversationEngine          — in-memory turn history, context window management
    ↓
IntentParser                — query → structured Intent object (researcher/Haiku)
    ↓
Pipeline Tools              — selective invocation based on intent
  ├── price_event()  ├── news()  ├── insider()  ├── macro()
  ├── fundamentals() ├── cross_market()  └── memory_search()
    ↓
┌─→ AnalysisLoop ──────────────────────────────────────────┐
│   Analyze data → evaluate confidence → if insufficient:  │
│   decide what's missing → call additional tool ──────────┘
│   Exit: confidence ≥ 0.7 | max 3 iterations | cost limit
    ↓
ResponseSynthesizer         — causal chain + adversarial check + conviction score
    ↓
SessionManager              — append turn to JSONL; trigger compaction if needed
```

### Intent Types

| Intent | Example Query | Tools Invoked |
|--------|---------------|---------------|
| `event_analysis` | "Why did AAPL spike 5% today?" | price_event, news, insider, cross_market |
| `deep_dive` | "Full analysis on TSMC" | price_event, fundamentals, news, insider, cross_market |
| `alpha_hunt` | "Where's the hidden alpha right now?" | macro, cross_market, news |
| `macro_query` | "Where are we in the rate cycle?" | macro |
| `cross_market` | "How does Korea semi data affect US AI stocks?" | cross_market, macro, price_event |
| `follow_up` | "What about insider trades?" | resolved from session context |

### LLM Role Mapping

| Component | Role | Default Model | Rationale |
|-----------|------|---------------|-----------|
| IntentParser | researcher | Claude Haiku | Lightweight classification; speed matters |
| AnalysisLoop | analyst | Claude Opus | Core reasoning; quality critical |
| ResponseSynthesizer (causal chain) | analyst | Claude Opus | Deep synthesis |
| ResponseSynthesizer (adversarial check) | strategist | Claude Opus | Contrarian judgment |
| SessionManager (compaction) | reporter | Claude Haiku | Summarization; cost matters |

### Tool Result Contract

All pipeline tools return a `ToolResult` subtype consumed uniformly by the AnalysisLoop:

```python
@dataclass
class ToolResult:
    tool: str
    success: bool
    data: dict
    source: str
    fetched_at: datetime
    is_stale: bool
    error: str | None  # populated if success=False; never raises silently
```

Failed tools are excluded from the evidence chain and flagged in the response caveat.

### Response Format

```
[ANALYSIS: {ticker/theme} — {date}]
Conviction: {score}/10

WHAT HAPPENED
{1-2 sentence direct answer}

EVIDENCE CHAIN
1. {evidence} — Source: {source}, {date}
   → leads to: {conclusion}

ADVERSARIAL CHECK
- {reason this signal could be wrong}
- {data staleness or reliability caveat}

VERDICT
{Final judgment with conviction score and key qualifier}
```

### Error Handling

| Failure | Behavior |
|---------|----------|
| Single tool fails | Exclude from evidence; note as "data unavailable" in ADVERSARIAL CHECK |
| ≥2 tools fail in one iteration | Exit loop early; caveat lists missing data |
| LLM call fails | Retry once; if still failing, return partial result with incompleteness note |
| Rate limit exhaustion mid-loop | Exit immediately; synthesize from data collected so far |
| JSONL write error | Log to stderr; still return response — never block on persistence |
| memory_search returns contradictory past analysis | Include both findings; flag conflict explicitly |

## Memory System

Session memory uses a three-tier architecture:

### Tier 1 — Raw Audit Log (JSONL)

Append-only per-session log. Every turn: user message, tool calls, tool results, agent response.

```jsonl
{"turn": 1, "role": "user", "content": "Why did AAPL spike?", "ts": "..."}
{"turn": 1, "role": "tool_call", "tool": "fetch_news", "args": {"ticker": "AAPL"}, "ts": "..."}
{"turn": 1, "role": "tool_result", "success": true, "source": "Finnhub", "ts": "..."}
{"turn": 1, "role": "assistant", "content": "...", "conviction": 8, "ts": "..."}
```

Complete audit trail — reconstructs exactly what the agent did and why.

### Tier 2 — Compressed Summary (Markdown)

When a session exceeds 8,000 tokens (measured via `tiktoken`), SessionManager compacts the conversation into a Markdown summary using the reporter role (Haiku). The summary replaces raw turns in the active context window; the JSONL is preserved.

### Tier 3 — Search Index (DuckDB)

DuckDB indexes all Tier 2 Markdown summaries for hybrid retrieval: keyword (FTS) + vector similarity (VSS/HNSW). Writes occur only at compaction time.

- Embedding model: `text-embedding-3-small` (OpenAI API) or `all-MiniLM-L6-v2` (local fallback). Configured in `TOOLS.md`.
- Tables: `session_index` (FTS), `session_embeddings` (HNSW).
- Source of truth is the Markdown files; DuckDB is the index only.

The agent calls `memory_search` autonomously when past context may be relevant.

### MEMORY.md vs. Tier 2

- **Tier 2**: auto-generated, per-session. Temporary working memory.
- **MEMORY.md**: cross-session long-term memory. Manually curated or auto-aggregated. Contains active theses and strong multi-session signals. Loaded at session start via `BOOTSTRAP.md`.

## Workspace

Agent configuration lives at `~/.tracer/`, loaded at session start.

```
~/.tracer/
├── IDENTITY.md      # Agent role and identity
├── SOUL.md          # Tone: cold, data-first, adversarial self-check
├── USER.md          # User preferences, risk tolerance, watchlist, cost budget
├── TOOLS.md         # Available providers, API key status, embedding model config
├── BOOTSTRAP.md     # Session init: load market context, watchlist, MEMORY.md
├── HEARTBEAT.md     # Session-start checks: watchlist re-evaluation, data freshness
├── MEMORY.md        # Cross-session long-term memory
└── memory/
    └── YYYY/MM/DD/
        ├── session-{id}.jsonl   ← Tier 1
        └── session-{id}.md      ← Tier 2
```

HEARTBEAT tasks run at session start (not as a background daemon). Results are summarized as a status message before the REPL hands control to the user.

## Coding Conventions

### SOLID Principles (Tracer 적용)

- **Single Responsibility**: 하나의 클래스/모듈은 하나의 역할만.
  - Adapter = 하나의 데이터 소스. Agent = 하나의 분석 역할.
  - "이 클래스가 변경되는 이유가 2개 이상이면 분리하라."
- **Open/Closed**: 새 adapter, 새 capability 추가 시 기존 코드를 수정하지 않는다.
  - Registry에 등록만 하면 동작해야 한다.
- **Liskov Substitution**: 모든 adapter는 해당 protocol을 완전히 대체 가능해야 한다.
  - FinnhubAdapter 대신 YfinanceAdapter를 넣어도 에이전트는 동일하게 동작.
- **Interface Segregation**: 에이전트는 필요한 capability만 요청한다.
  - "모든 데이터를 다 가져오는" 범용 인터페이스를 만들지 않는다.
- **Dependency Inversion**: 에이전트는 Protocol(추상)에 의존. 구체 adapter를 직접 참조하지 않는다.
  - `def analyze(provider: PriceProvider)` O
  - `def analyze(provider: FinnhubAdapter)` X

### DRY (Don't Repeat Yourself)

- 동일한 로직이 2곳 이상에서 반복되면 함수로 추출한다.
- 단, 3번 이상 반복될 때 추출을 고려. 2번은 허용할 수 있다 (premature abstraction 방지).
- 공유 유틸리티는 `src/tracer/utils/`에 둔다.
- adapter 간 공통 로직(rate limit 핸들링, 재시도 등)은 base class나 mixin으로 추출.

### Comments & Docstrings

- **Code explains what. Comments explain why.** Use clear variable and function names — no comment needed for obvious logic.
- **Write a comment only when the reason behind the code is not self-evident.**
  - Good: `# Finnhub returns data with 30-min post-close delay — set a longer cache TTL`
  - Bad: `# get the price`
- **Keep comments minimal.** Too many comments are noise. If a comment restates the code, delete it.
- **Docstrings:** Required on all public functions and classes. Skip private functions unless the logic is non-obvious. Use Google style.
  ```python
  async def get_price(self, ticker: str) -> float:
      """Fetch the current stock price.

      Args:
          ticker: Stock symbol (e.g., "AAPL", "005930.KS")

      Returns:
          Current price in USD.

      Raises:
          ProviderError: If the API call fails.
          RateLimitError: If the request limit is exceeded.
      """
  ```
- **TODO/FIXME:**
  - `# TODO: {description}` — deferred implementation
  - `# FIXME: {description}` — known issue, needs a fix
  - Include issue number when available: `# TODO(#42): add cache expiry logic`

### Language

- **All project content must be written in English.** This includes code, comments, docstrings, commit messages, and documentation.
- Korean is acceptable only in user-facing conversational output (CLI prompts, report narratives directed at end users).

### General

- Type hints on all public functions.
- Use `async/await` for I/O-bound operations (API calls).
- All adapter implementations must fully implement the corresponding protocol.
- Config-driven: provider selection, model assignment, API keys via environment variables.
- Keep files under 300 LOC; split when exceeded.
- Tests mirror `src/` structure. Minimum: one test per adapter, one per agent.
- Naming: snake_case for functions/variables, PascalCase for classes, UPPER_CASE for constants.

## Git Conventions

- Conventional Commits: `feat|fix|refactor|build|ci|chore|docs|style|perf|test`
- Keep commits atomic and focused.
- Branch naming: `feat/<name>`, `fix/<name>`, `refactor/<name>`
- Do not commit API keys or secrets. Use `.env` (gitignored).

## Development Workflow

1. Read this file before starting any work.
2. Run `uv sync` to install dependencies.
3. Run `ruff check . && ruff format --check .` before committing.
4. Run `pytest` before pushing.
5. Run `pyright` for type checking.
