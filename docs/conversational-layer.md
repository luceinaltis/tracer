# Conversational Layer

Users interact with Tracer via natural language queries. The conversational layer wraps the Tracer Cycle steps as discrete tools, manages session context, and routes each query to the appropriate pipeline steps.

```text
CLI (REPL)
    ↓
ConversationEngine          — in-memory turn history, context window management
    ↓
IntentParser                — query → structured Intent object (researcher/Haiku)
    ↓
Pipeline Tools              — selective invocation based on intent
  ├── price_event()
  ├── news()
  ├── insider()
  ├── macro()
  ├── fundamentals()
  ├── cross_market()
  └── memory_search()
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

## Intent Types

| Intent | Example Query | Tools Invoked |
|--------|---------------|---------------|
| `event_analysis` | "Why did AAPL spike 5% today?" | price_event, news, insider, cross_market |
| `deep_dive` | "Full analysis on TSMC" | price_event, fundamentals, news, insider, cross_market |
| `alpha_hunt` | "Where's the hidden alpha right now?" | macro, cross_market, news |
| `macro_query` | "Where are we in the rate cycle?" | macro |
| `cross_market` | "How does Korea semi data affect US AI stocks?" | cross_market, macro, price_event |
| `follow_up` | "What about insider trades?" | resolved from session context |

## LLM Role Mapping

All role-to-model assignments are overridable via config.

| Role | Description | Default Model | Used By |
|------|-------------|---------------|---------|
| researcher | Gather and summarize market data | Claude Sonnet | IntentParser, Universe Screening, Consensus Mapping |
| analyst | Deep financial/cross-market analysis | Claude Opus | AnalysisLoop, ResponseSynthesizer (causal chain) |
| strategist | Investment decision and signal gen | Claude Opus | ResponseSynthesizer (adversarial check), Contrarian Detection, Conviction Scoring |
| reporter | Summary and report generation | Claude Haiku | SessionManager (compaction), Alpha Report |

## Tool Result Contract

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

## Response Format

```text
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

## Error Handling

| Failure | Behavior |
|---------|----------|
| Single tool fails | Exclude from evidence; note as "data unavailable" in ADVERSARIAL CHECK |
| ≥2 tools fail in one iteration | Exit loop early; caveat lists missing data |
| LLM call fails | Retry once; if still failing, return partial result with incompleteness note |
| Rate limit exhaustion mid-loop | Exit immediately; synthesize from data collected so far |
| JSONL write error | Log to stderr; still return response — never block on persistence |
| memory_search returns contradictory past analysis | Include both findings; flag conflict explicitly |
