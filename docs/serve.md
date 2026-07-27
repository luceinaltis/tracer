# qracer serve — Background Service

Long-running service that executes scheduled tasks and checks price alerts without an interactive REPL session.

## Architecture

```text
~/.qracer/ (shared state)
├── tasks.json       ← TaskStore (hot-reloaded on mtime change)
├── alerts.json      ← AlertStore (hot-reloaded on mtime change)
├── serve.pid        ← PID file (prevents duplicate instances)
├── config.toml
├── providers.toml
├── portfolio.toml
└── credentials.env

[Process 1] qracer serve       — task executor + alert monitor + notifications
[Process 2] qracer repl        — interactive session (can create tasks/alerts)
[Process 3] qracer web (향후)   — web dashboard API
```

All processes share `~/.qracer/` files. Stores detect external modifications via mtime and reload automatically.

## Usage

```bash
# Start the service
qracer serve

# Custom check interval (default 30s)
qracer serve --check-interval 10

# Runs until Ctrl+C or SIGTERM
```

## What it does

Each tick (every 1 second, throttled by per-monitor check intervals):

1. **Alert check** (`AlertMonitor`): active price alerts → notify on match
2. **Task check** (`TaskExecutor`): due scheduled tasks → notify on failure ([schedule.md](schedule.md))
3. **Autonomous monitor** (`AutonomousMonitor`): during US market hours, scans the
   watchlist for significant price moves / breaking news
4. **Custom-agent monitor** (`AgentMonitor`): runs due cron/continuous custom agents
   ([custom-agents.md](custom-agents.md))

Notifications are sent via configured channels (e.g., Telegram). Configure in `~/.qracer/credentials.env`:
```bash
TELEGRAM_BOT_TOKEN=your-token
TELEGRAM_CHAT_ID=your-chat-id
```

## Multi-process safety

- **PID file** (`serve.pid`): Prevents running two `qracer serve` instances
- **mtime hot-reload**: TaskStore and AlertStore re-read from disk when another process modifies the JSON file
- REPL and serve can run simultaneously — REPL creates tasks/alerts, serve executes them

## Components

| File | Role |
|------|------|
| `qracer/server.py` | Server loop (asyncio event loop with tick) |
| `qracer/pidfile.py` | PID file acquire/release/check |
| `qracer/tasks.py` | TaskStore with mtime hot-reload |
| `qracer/alerts.py` | AlertStore with mtime hot-reload |
| `qracer/autonomous.py` | AutonomousMonitor (market-hours watchlist scan) |
| `qracer/agent_monitor.py` | AgentMonitor (custom agents) |
| `qracer/notifications/` | Notification fan-out (Telegram, etc.) |
