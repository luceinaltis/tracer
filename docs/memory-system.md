# Memory System

Session memory uses a three-tier architecture:

## Tier 1 — Raw Audit Log (JSONL)

Append-only per-session log. Every turn: user message, tool calls, tool results, agent response.

```jsonl
{"turn": 1, "role": "user", "content": "Why did AAPL spike?", "ts": "..."}
{"turn": 1, "role": "tool_call", "tool": "fetch_news", "args": {"ticker": "AAPL"}, "ts": "..."}
{"turn": 1, "role": "tool_result", "success": true, "source": "Finnhub", "ts": "..."}
{"turn": 1, "role": "assistant", "content": "...", "conviction": 8, "ts": "..."}
```

Complete audit trail — reconstructs exactly what the agent did and why.

## Tier 2 — Compressed Summary (Markdown)

When a session exceeds 8,000 tokens (rough `len // 4` estimate of the JSONL log), the `ConversationEngine` invokes `SessionCompactor.compact_and_save()` after each turn via `_maybe_compact()`. The reporter role (Haiku) condenses the turns into a concise Markdown summary, which is written to `~/.qracer/summaries/<session_id>.md`. The raw JSONL log is preserved untouched.

## Tier 3 — Search Index (DuckDB)

`MemorySearcher` indexes Tier 2 Markdown summaries in DuckDB for hybrid retrieval: keyword (BM25 via FTS) and, when an embedding function is supplied, vector similarity via DuckDB's `list_cosine_similarity`. The two branches are fused with reciprocal rank fusion so scores from different scales can be combined without normalisation.

- Embedding is pluggable via the `embedding_fn: Callable[[str], list[float]]` parameter — callers can back it with the Claude API, `text-embedding-3-small`, `sentence-transformers`, or any other model. When `embedding_fn` is `None` the searcher falls back to keyword-only search.
- Tables: `session_index` (FTS) and `session_embeddings` (cosine similarity).
- Source of truth is the Markdown files; DuckDB is the index only.

The agent calls `memory_search` autonomously when past context may be relevant.

## Cross-Session Loading

On `qracer repl` startup, the CLI instantiates a file-backed `MemorySearcher` at `~/.qracer/memory_index.duckdb` and re-indexes every Markdown file in `~/.qracer/summaries/`. The number of loaded contexts is printed to the user so returning sessions immediately know how much prior memory is in scope.

## Structured cross-session facts (FactStore)

Beyond the three memory tiers (which are conversational), durable investment facts
live in `FactStore` (`memory/fact_store.py`, DuckDB at `~/.qracer/fact_store.duckdb`).
It persists **theses**: `save_thesis` records a trade thesis and auto-supersedes a
prior open thesis on the same ticker; `get_open_theses` re-injects them as prior
evidence in the deep path (see [pipeline.md](pipeline.md)).

Today theses are only ever created and superseded — there is no outcome tracking
(`update_thesis_status` is unused in production). A thesis lifecycle
(hit/stopped/invalidated) and a hit-rate feedback loop are on the roadmap in
[architecture.md](architecture.md).
