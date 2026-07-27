# AGENTS.md

## Project Overview

qracer — conversational, CLI-first investment-research tool.
Users query in natural language; the engine parses intent, fetches data through a
capability registry, and synthesizes a response with an LLM. It also runs a
background daemon (`qracer serve`) for scheduled tasks, price alerts, and custom
autonomous agents.

## Agent Guidelines

Before starting any task, check `.claude/skills/` for a matching skill and follow it.
Skills define workflows for feature dev, PR review, testing, refactoring, and more — always use them over ad-hoc approaches.

## Design Documents

Before implementing any feature or making architectural changes, read the relevant docs first:

| Document | When to read |
|----------|-------------|
| `docs/architecture.md` | Any new feature or structural change (layer map + roadmap) |
| `docs/pipeline.md` | Query flow, handlers, analysis loop, tools |
| `docs/conversational-layer.md` | Intent, context, roles, response formats |
| `docs/risk-system.md` | Risk management, position sizing, portfolio limits |
| `docs/memory-system.md` | Memory or state management changes |
| `docs/serve.md` / `docs/schedule.md` | Background daemon, scheduled tasks, alerts |
| `docs/custom-agents.md` | Prompt-defined autonomous agents |

## Tech Stack

- **Language**: Python 3.12+
- **Package manager**: uv
- **Linter/Formatter**: ruff
- **Type checker**: pyright
- **Test**: pytest
- **LLM**: multi-provider (Claude, OpenAI, Gemini, OpenRouter)
- **Data**: multi-source (yfinance, Finnhub, FRED)
- **Storage**: DuckDB + file-backed JSON stores
