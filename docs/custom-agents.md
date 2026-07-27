# Custom Agents

User-defined autonomous agents: each is an **OpenRouter model + a free-form prompt**,
run manually, on a cron schedule, or continuously. Results are stored per agent for
later inspection. This is a self-contained layer — it does **not** touch the data
tools, risk engine, or the conversation pipeline (see the roadmap note below).

## Model

An agent is defined by `Agent` (`qracer/agents_store.py`):

| Field | Meaning |
|---|---|
| `name`, `model`, `prompt` | display name, OpenRouter model id, system prompt |
| `trigger_type` | `manual` / `cron` / `continuous` |
| `cron` | croniter expression (when `trigger_type == cron`) |
| `enabled` | on/off |
| `next_run_at`, `last_run_at`, `last_output`, `last_error`, `run_count` | runtime state |

Agents are persisted as `~/.qracer/agents.json` via `AgentStore` (file-backed, mtime
hot-reload — the daemon, CLI, and web UI share one file).

## Execution

- **Runner** (`agent_runner.py`): `run_agent` / `run_agents` call
  `LLMProvider.complete` with `CompletionRequest.model = agent.model` (OpenRouter
  honors it). Failures are captured per agent, so one failing agent never blocks the
  others when several run concurrently.
- **Monitor** (`agent_monitor.py`): plugs into the `qracer serve` tick loop
  ([serve.md](serve.md)). Each cycle it runs the due agents concurrently
  (`asyncio.gather`) and persists their output. `cron` agents advance via croniter;
  `continuous` agents run each cycle subject to a cooldown.

## Surfaces

| Surface | How |
|---|---|
| Configure | `qracer web` → NiceGUI page at `http://127.0.0.1:8000/` (name, model, prompt, trigger) |
| Run now | `qracer run` (all enabled) / `qracer run --agent NAME` / `qracer run "query"` |
| REPL | `/run` (all enabled) / `/run <text>` |
| Autonomous | `qracer serve` runs cron/continuous agents; view results in the web Results tab |

## Requirements

- OpenRouter enabled in `providers.toml` with `OPENROUTER_API_KEY` set.
- The `web` extra installed for the config UI (`pip install 'qracer[web]'`, which
  includes NiceGUI); `openrouter` extra for the OpenAI client.

## Roadmap

The agents are currently pure LLM + prompt with no market-data access. To make them
useful for grounded advice they need the `DataRegistry` + tool pipeline injected, so
a scheduled agent can produce cited alerts (e.g. "scan my portfolio's news events
each morning and propose sizing changes with evidence"). This is Phase 4 of the
investor-harness roadmap in [architecture.md](architecture.md).
