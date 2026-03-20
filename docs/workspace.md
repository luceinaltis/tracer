# Workspace

Agent configuration lives at `~/.tracer/`, loaded at session start.

```text
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

## HEARTBEAT Execution Model

HEARTBEAT tasks run at session start (not as a background daemon). When the CLI REPL starts, it reads `HEARTBEAT.md` and executes any pending checks before handing control to the user.

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

Results are summarized as a brief status message at the start of each session. Results are appended to the day's JSONL log.
