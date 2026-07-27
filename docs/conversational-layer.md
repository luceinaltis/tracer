# Conversational Layer

Intent parsing, context tracking, roles, and response formats. For the end-to-end
flow see [pipeline.md](pipeline.md).

```text
CLI REPL
  → ConversationEngine   — context extraction, routing, fact persistence, compaction
  → IntentParser         — classify into an Intent
  → handler              — Portfolio / QuickPath / Comparison / Standard
  → synthesizer          — format the response
  → SessionLogger        — persist the turn (JSONL)
```

## Intent parsing

`conversation/intent.py`. `IntentParser.parse()` classifies a query (LLM classify
with a keyword fallback) into an `Intent`:

```python
@dataclass
class Intent:
    intent_type: IntentType
    tickers: list[str]
    tools: list[str]        # default tools from INTENT_TOOL_MAP
    raw_query: str
```

`INTENT_TOOL_MAP` maps each `IntentType` to its default tool list; the
`AnalysisLoop` may request more tools during the deep path. An import-time check
keeps `INTENT_TOOL_MAP` and the dispatcher's `TOOL_DISPATCH` in sync.

## Context & follow-up resolution

The engine extracts a `ConversationContext` from recent turns (active tickers,
topic, recent tool results). When a query has no explicit ticker, the engine tries
to resolve it from context (pronouns, last topic); if still ambiguous it **asks**
rather than guesses.

## Roles

`llm/providers.py` defines four roles. In the live pipeline they are used as:

| Role | Used by |
|---|---|
| `RESEARCHER` | intent parsing |
| `ANALYST` | analysis-loop evaluation |
| `STRATEGIST` | trade thesis + response synthesis |
| `REPORTER` | session compaction |

`LLMRegistry` (`llm/registry.py`) routes a role to a registered provider. Adapters
carry a `DEFAULT_MODEL_MAP` for per-role model tiering, **but** the role is not
passed into `provider.complete()` today, so every call falls back to the provider's
default model — the tiering does not currently take effect. Fixing this is on the
roadmap ([architecture.md](architecture.md)).

> The four classes under `qracer/agents/` (Researcher/Analyst/Strategist/Reporter)
> are legacy scaffolding — they are **not** wired into the live pipeline, which calls
> roles directly via the registry.

## Tool result contract

Every tool returns a uniform `ToolResult` (`models/base.py`):

```python
@dataclass
class ToolResult:
    tool: str
    success: bool
    data: dict
    source: str
    fetched_at: datetime
    is_stale: bool
    error: str | None
```

`source`/`fetched_at` reach the LLM prompt as evidence labels. Note `is_stale` is
currently computed at fetch time and results are not cached between turns, so it is
effectively always `False` — data-age grounding is a roadmap item.

## Response formats

### Quick answer (QuickPath)
Template line(s) for price checks and simple lookups — no LLM.

### Comparison (ComparisonHandler)
Side-by-side markdown table across tickers plus a comparative verdict.

### Full analysis (StandardHandler)
`ResponseSynthesizer` renders a fixed template:

```text
Conviction: {score}/10

WHAT HAPPENED
{direct answer}

EVIDENCE CHAIN
{claim} — source: {source}

ADVERSARIAL CHECK
{why this could be wrong; data caveats}

VERDICT
{judgment + qualifier}
```

The template asks for an evidence chain and adversarial check, but does not yet
enforce that numbers cite a specific `ToolResult` — the grounded, source-cited
contract is the core of the harness roadmap.
