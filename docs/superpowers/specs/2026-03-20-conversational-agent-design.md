# Tracer Conversational Agent — Design Spec
> Created: 2026-03-20

## Overview

Tracer is a conversational investment agent for professional investors.
Users query in natural language; the agent fetches real data, builds a causal reasoning chain, and responds with calibrated conviction — never hallucinating, always adversarially self-checking.

**Core differentiators vs. existing agents:**
- No hallucination: every claim is backed by a real API call with source + timestamp
- Causal chains: not just facts, but "A → B → C → investment conclusion"
- Adversarial self-check: agent argues against its own conclusion before committing
- Autonomous reasoning loop: agent decides when it needs more data and self-queries
- Long-term memory: past analysis is searchable and feeds into current sessions

---

## 1. Architecture

### Approach: Structured Pipeline + Conversational Layer

Tracer Cycle steps are wrapped as discrete **tools**. A conversational layer manages session context and selects which tools to invoke per query. This preserves data integrity (structured pipeline) while enabling natural multi-turn dialogue.

```
CLI (REPL)
    ↓
ConversationEngine          — in-memory turn history, context window management
    ↓
IntentParser                — query → structured Intent object
    ↓
Pipeline Tools              — selective invocation based on intent
  ├── price_event()         → PriceEventResult
  ├── news()                → NewsResult
  ├── insider()             → InsiderResult
  ├── macro()               → MacroResult
  ├── fundamentals()        → FundamentalsResult
  ├── cross_market()        → CrossMarketResult
  └── memory_search()       → MemorySearchResult
    ↓
┌─→ AnalysisLoop ─────────────────────────────────────┐
│       ↓                                              │
│   Analyze current data (analyst role — Opus)         │
│       ↓                                              │
│   Evaluate confidence (heuristic: evidence count,    │
│                        source diversity, data age)   │
│       ├── sufficient ──────────────────────────────→ ResponseSynthesizer
│       ├── max 3 iterations or cost limit ──────────→ ResponseSynthesizer (with caveat)
│       └── insufficient → decide what's missing      │
│                   ↓                                  │
│               call additional tool ──────────────────┘
    ↓
ResponseSynthesizer         — causal chain + adversarial check + conviction score
    ↓
SessionManager              — appends turn to JSONL; triggers compaction if needed
```

### Intent Types

| Intent | Example Query | Tools Invoked |
|--------|---------------|---------------|
| `event_analysis` | "Why did AAPL spike 5% today?" | price_event, news, insider, cross_market |
| `deep_dive` | "Full analysis on TSMC" | price_event, fundamentals, news, insider, cross_market |
| `alpha_hunt` | "Where's the hidden alpha right now?" | macro, cross_market, news |
| `macro_query` | "Where are we in the rate cycle?" | macro |
| `cross_market` | "How does Korea semi data affect US AI stocks?" | cross_market, macro, price_event |
| `follow_up` | "What about insider trades?" | resolved from session context; calls insider |

### Intent Object

`IntentParser` returns a typed `Intent` dataclass:

```python
@dataclass
class Intent:
    type: IntentType                  # enum: event_analysis | deep_dive | ...
    ticker: str | None                # primary ticker if applicable
    tickers: list[str]                # additional tickers (cross-market)
    time_horizon: str | None          # short | medium | long
    context_ref: int | None           # turn index this follow-up references
    raw_query: str
```

`IntentParser` uses the **researcher** role (Claude Haiku) — lightweight classification, not deep analysis.

### Tool Result Contract

All pipeline tools return a `ToolResult` subtype. The `AnalysisLoop` and `ResponseSynthesizer` consume these uniformly:

```python
@dataclass
class ToolResult:
    tool: str                         # tool name
    success: bool
    data: dict                        # tool-specific payload
    source: str                       # data provider name
    fetched_at: datetime
    is_stale: bool                    # True if data exceeds freshness threshold
    error: str | None                 # populated if success=False

# Example subtype
@dataclass
class InsiderResult(ToolResult):
    trades: list[InsiderTrade]
    filing_type: str                  # e.g. "10b5-1" vs. "non-scheduled"
```

If `success=False`, the tool returns the `ToolResult` with `error` populated — it never raises silently. The `AnalysisLoop` excludes failed tool results from the evidence chain and flags them in the caveat.

### Autonomous Reasoning Loop

The agent evaluates confidence after each data fetch. Confidence is a heuristic scoring:
- Number of independent confirming sources
- Data recency (freshness threshold per data type)
- Source diversity (price + news + alternative = higher than price alone)

If confidence is insufficient, the agent identifies the weakest link in the current evidence and calls one additional tool to fill it.

**Exit conditions:**
- Confidence score ≥ threshold (configurable, default: 0.7)
- Maximum 3 loop iterations reached
- Aggregate API call budget for the session exhausted

When exiting due to a limit, the response includes an explicit caveat noting incomplete data coverage.

**LLM role in loop:** The confidence evaluation and "what data is missing" judgment uses the **analyst** role (Claude Opus).

---

## 2. LLM Role Mapping

Each component uses a specific LLM role from the existing registry:

| Component | Role | Default Model | Rationale |
|-----------|------|---------------|-----------|
| IntentParser | researcher | Claude Haiku | Lightweight classification; speed matters |
| AnalysisLoop (confidence eval) | analyst | Claude Opus | Core reasoning; quality critical |
| AnalysisLoop (what's missing) | analyst | Claude Opus | Same reasoning context |
| ResponseSynthesizer (causal chain) | analyst | Claude Opus | Deep synthesis |
| ResponseSynthesizer (adversarial check) | strategist | Claude Opus | Contrarian judgment |
| SessionManager (compaction) | reporter | Claude Haiku | Summarization; cost matters |

All role-to-model assignments are overridable via config.

---

## 3. Response Format

Every response follows a consistent structure regardless of query type:

```
[ANALYSIS: {ticker/theme} — {date}]
Conviction: {score}/10

WHAT HAPPENED
{1-2 sentence direct answer}

EVIDENCE CHAIN
1. {evidence} — Source: {source}, {date}
2. {evidence} — Source: {source}, {date}
   → leads to: {conclusion}

ADVERSARIAL CHECK
- {reason this signal could be wrong}
- {data staleness or reliability caveat}

VERDICT
{Final judgment with conviction score and key qualifier}
```

**Design rules:**
- Every data point must cite source and timestamp
- If data is unavailable, state it explicitly — never estimate
- Adversarial check is mandatory — at least one counter-argument per response
- Conviction 1-4: weak signal only; 5-7: developing; 8-10: actionable

---

## 4. Error Handling

| Failure | Behavior |
|---------|----------|
| Single tool call fails | Exclude from evidence chain; note in ADVERSARIAL CHECK as "data unavailable" |
| Multiple tools fail in same loop iteration | If ≥2 tools fail, exit loop early; response includes caveat listing missing data |
| LLM call fails (IntentParser) | Retry once; if still failing, return error message to user and abort session turn |
| LLM call fails (AnalysisLoop/Synthesizer) | Retry once; if still failing, return partial result with explicit incompleteness note |
| Rate limit exhaustion mid-loop | Exit loop immediately; synthesize from data collected so far; caveat rate limit |
| Session persistence failure (JSONL write error) | Log error to stderr; still return response to user — never block on persistence |
| memory_search returns contradictory past analysis | Include both past and current findings; flag the conflict explicitly in response |

---

## 5. Memory System (Three-Tier)

### Tier 1 — Raw Audit Log (JSONL)

Append-only log of every turn: user message, tool calls, tool results, agent response.

```jsonl
{"turn": 1, "role": "user", "content": "Why did AAPL spike?", "ts": "2026-03-20T14:32:01Z"}
{"turn": 1, "role": "tool_call", "tool": "fetch_news", "args": {"ticker": "AAPL"}, "ts": "..."}
{"turn": 1, "role": "tool_result", "tool": "fetch_news", "success": true, "source": "Finnhub", "ts": "..."}
{"turn": 1, "role": "assistant", "content": "...", "conviction": 8, "ts": "..."}
```

**Purpose:** complete audit trail — reconstructs exactly what the agent did and why.
**Ownership:** SessionManager writes to JSONL on every turn append.

### Tier 2 — Compressed Summary (Markdown, per session)

When a session exceeds **8,000 tokens** (measured by the ConversationEngine using `tiktoken` with the active model's encoding), SessionManager triggers compaction using the reporter role (Haiku). The summary replaces the raw turns in the active context window.

```markdown
# Session 2026-03-20-143201

## Key Findings
- AAPL: CEO insider buy confirmed ($2.1M), TSMC supply chain signal positive
- Conviction 8/10 — earnings revision upside likely in Q2

## Open Threads
- AI chip partnership news unverified — monitor for official announcement

## Data Used
- Finnhub (price, news, insider), FRED (macro)
```

**Scope:** per-session. The raw JSONL is preserved; the Markdown is the condensed version for future context loading.

### Tier 3 — Search Index (SQLite)

SQLite indexes all Tier 2 Markdown summaries for hybrid retrieval: **keyword (FTS5) + vector similarity**.

- **Embedding model:** `text-embedding-3-small` (OpenAI) via API, or `all-MiniLM-L6-v2` (local, via `sentence-transformers`) as fallback. Configurable in `TOOLS.md`.
- Embeddings are generated when a Markdown summary is written (at compaction time).
- SQLite stores embeddings as BLOB; similarity search uses cosine distance in Python (not in-DB).

The agent calls `memory_search` autonomously when it judges past context is relevant:

```
User: "Analyze TSMC"
  → memory_search("TSMC") → finds 2-week-old session summary
  → prior conviction was 6, supply chain concern noted
  → current analysis includes delta: "conviction moved 6→8 since last analysis, why"
```

SQLite is the search index only — source of truth is the Markdown files.

**No retention policy in v1.** Files accumulate. Archival/cleanup is a future concern.

### MEMORY.md vs. Tier 2

- **Tier 2 (per-session Markdown):** auto-generated, per session, temporary working memory.
- **MEMORY.md:** manually curated or auto-aggregated cross-session long-term memory. Contains active investment theses, strong multi-session signals, and notes the user wants permanently retained. The agent reads `MEMORY.md` at session start via `BOOTSTRAP.md`. It may reference Tier 2 summaries but is a separate artifact.

---

## 6. Workspace Structure

Agent configuration lives at `~/.tracer/` and is loaded at session start via `BOOTSTRAP.md`.

```
~/.tracer/
├── IDENTITY.md      # "You are Tracer, a professional investment analyst agent"
├── SOUL.md          # Tone: cold, data-first, adversarial self-check, no false confidence
├── USER.md          # User investment preferences, risk tolerance, preferred markets
├── TOOLS.md         # Available data providers, API key status, embedding model config
├── BOOTSTRAP.md     # Session init: load market context, watchlist, MEMORY.md
├── HEARTBEAT.md     # Periodic task definitions (see below)
├── MEMORY.md        # Cross-session long-term memory: active theses, strong signals
│
└── memory/
    └── 2026/
        └── 03/
            └── 20/
                ├── session-143201.jsonl   ← Tier 1
                └── session-143201.md      ← Tier 2
    index.db                               ← Tier 3 SQLite index
```

### HEARTBEAT Execution Model

HEARTBEAT tasks run **at session start** (not as a background daemon). When the CLI REPL starts, it reads `HEARTBEAT.md` and executes any pending checks before handing control to the user.

`HEARTBEAT.md` defines tasks as structured entries:

```markdown
## Watchlist Signal Check
- frequency: daily
- last_run: 2026-03-19
- action: re-evaluate conviction scores for all tickers in USER.md watchlist

## Data Freshness Check
- frequency: session_start
- action: verify Finnhub and FRED data is within acceptable staleness threshold
```

HEARTBEAT results are summarized as a brief status message at the start of each session. Results are appended to the day's JSONL log.

---

## 7. Storage Boundaries

| Store | Contents | Write Pattern |
|-------|----------|---------------|
| DuckDB (`tracer.db`) | Market data: prices, fundamentals, macro, news, signals | Analytical append (market data pipeline) |
| JSONL (`memory/.../session-*.jsonl`) | Conversational audit trail: turns, tool calls, LLM responses | Transactional append (every turn) |
| Markdown (`memory/.../session-*.md`) | Compressed session summaries (Tier 2) | Write-once at compaction |
| SQLite (`memory/index.db`) | Embedding + keyword index over Markdown files | Updated at compaction |

**`agent_logs` in DuckDB (from AGENTS.md):** stores pipeline execution metadata (which Tracer Cycle steps ran, timing, data source used). This is distinct from conversational turns. Both exist: DuckDB tracks pipeline runs; JSONL tracks conversation turns. No overlap.

**Signals:** generated signals are written to DuckDB `signals` table (persistent, queryable across sessions) AND referenced in the session JSONL (for audit). JSONL is the log; DuckDB is the source of truth for signals.

---

## 8. Project Structure Changes

New modules added to `src/tracer/`:

```
src/tracer/
├── conversation/
│   ├── engine.py        # ConversationEngine — in-memory turn history, context window mgmt
│   ├── intent.py        # IntentParser — query → Intent dataclass (researcher/Haiku)
│   ├── synthesizer.py   # ResponseSynthesizer — causal chain + adversarial check (analyst/Opus)
│   └── session.py       # SessionManager — JSONL append, compaction trigger, Markdown write
├── memory/
│   ├── search.py        # memory_search tool — hybrid FTS5 + vector retrieval
│   └── index.py         # SQLite index management — embedding generation, upsert
├── tools/               # Pipeline tool wrappers (one file per tool)
│   ├── price_event.py   # → PriceEventResult
│   ├── news.py          # → NewsResult
│   ├── insider.py       # → InsiderResult
│   ├── macro.py         # → MacroResult
│   ├── fundamentals.py  # → FundamentalsResult (used by deep_dive, event_analysis)
│   └── cross_market.py  # → CrossMarketResult
├── agents/              # existing
├── llm/                 # existing
├── data/                # existing
└── storage/             # existing (DuckDB — market data only)
```

CLI entry point: `scripts/tracer.py` — REPL loop, sends each message to `ConversationEngine`.

**Ownership boundary:**
- `ConversationEngine` owns: in-memory turn list, context window token counting, loading BOOTSTRAP/MEMORY.md at start.
- `SessionManager` owns: JSONL file writes, compaction trigger (called by ConversationEngine when token count ≥ 8K), Markdown file writes.

---

## 9. Key Design Constraints

- **No hallucination:** if an API call fails or data is unavailable, the agent states it explicitly and excludes it from the conclusion. No estimation or gap-filling.
- **Dependency inversion:** tools accept capability protocols (`PriceProvider`), never concrete adapters (`FinnhubAdapter`).
- **Cost-aware loop:** each additional tool call in the reasoning loop must clear a value > cost threshold. Cost budget is configurable in `USER.md` (per-session API call limit).
- **Storage separation:** DuckDB for market data and signals. JSONL for conversation audit. SQLite for session search index only.
- **CLI first:** core engine is interface-agnostic. Chat UI or API layer can be added later without touching the core.
