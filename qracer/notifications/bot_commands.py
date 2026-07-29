"""Telegram bot-command dispatch — the inbound side of ``qracer serve``.

Extracted from ``Server`` so the daemon tick loop and the bot's request/reply UI
are separate concerns. ``Server`` composes a :class:`BotCommandHandler` and forwards
polled commands to :meth:`dispatch`; the ``/status`` reply is provided by a callback
(uptime and channel state live on the Server).
"""

from __future__ import annotations

from collections.abc import Callable

from qracer.alerts import AlertCondition, AlertStore
from qracer.notifications.telegram_poller import BotCommand
from qracer.tasks import TaskActionType, TaskStore


class BotCommandHandler:
    """Routes an inbound :class:`BotCommand` to a reply string."""

    def __init__(
        self,
        alert_store: AlertStore,
        task_store: TaskStore,
        status_provider: Callable[[], str],
    ) -> None:
        self._alert_store = alert_store
        self._task_store = task_store
        self._status_provider = status_provider

    def dispatch(self, command: BotCommand) -> str:
        """Route a command to its handler; handlers return the reply text."""
        action = command.action
        if action in {"help", "start"}:
            return self._cmd_help()
        if action == "status":
            return self._status_provider()
        if action == "alerts":
            return self._cmd_alerts()
        if action == "alert":
            return self._cmd_create_alert(command.args)
        if action == "tasks":
            return self._cmd_tasks()
        if action == "schedule":
            return self._cmd_schedule(command.args)
        if action in {"analyze", "portfolio"}:
            return (
                f"/{action} is not supported in bot mode yet — "
                "use the qracer CLI on the host. Try /help."
            )
        return f"Unknown command: /{action}. Try /help."

    @staticmethod
    def _cmd_help() -> str:
        return (
            "qracer bot commands:\n"
            "/status — server status and uptime\n"
            "/alerts — list active price alerts\n"
            "/alert TICKER above|below PRICE — create a price alert\n"
            "/tasks — list scheduled tasks\n"
            "/schedule ACTION TICKER SCHEDULE — schedule a task\n"
            "    e.g. /schedule analyze AAPL every 1h\n"
            "/help — show this message"
        )

    def _cmd_alerts(self) -> str:
        alerts = self._alert_store.get_active()
        if not alerts:
            return "No active alerts."
        lines = ["Active alerts:"]
        for a in alerts:
            lines.append(f"  {a.id}  {a.describe()}")
        return "\n".join(lines)

    def _cmd_create_alert(self, args: list[str]) -> str:
        if len(args) < 3:
            return "Usage: /alert TICKER above|below PRICE  (e.g. /alert AAPL above 200)"
        ticker, condition_str, price_str = args[0], args[1].lower(), args[2]
        try:
            condition = AlertCondition(condition_str)
        except ValueError:
            return f"Unknown condition '{condition_str}'. Use 'above' or 'below'."
        if condition is AlertCondition.CHANGE_PCT:
            return "Use 'above' or 'below' from the bot — change_pct alerts need the CLI."
        try:
            threshold = float(price_str)
        except ValueError:
            return f"Invalid price '{price_str}' — must be a number."
        alert = self._alert_store.create(ticker, condition, threshold)
        return f"Created alert {alert.id}: {alert.describe()}"

    def _cmd_tasks(self) -> str:
        tasks = self._task_store.get_active()
        if not tasks:
            return "No scheduled tasks."
        lines = ["Scheduled tasks:"]
        for t in tasks:
            lines.append(f"  {t.id}  {t.describe()}")
        return "\n".join(lines)

    def _cmd_schedule(self, args: list[str]) -> str:
        if len(args) < 3:
            return (
                "Usage: /schedule ACTION TICKER SCHEDULE\n"
                "  ACTION: analyze | news_scan | portfolio_snapshot\n"
                "  e.g. /schedule analyze AAPL every 1h"
            )
        action_str = args[0].lower()
        ticker = args[1].upper()
        schedule_spec = " ".join(args[2:])
        try:
            action_type = TaskActionType(action_str)
        except ValueError:
            valid = ", ".join(t.value for t in TaskActionType)
            return f"Unknown action '{action_str}'. Valid: {valid}"
        try:
            task = self._task_store.create(action_type, {"ticker": ticker}, schedule_spec)
        except ValueError as exc:
            return f"Invalid schedule: {exc}"
        return f"Scheduled task {task.id}: {task.describe()}"


__all__ = ["BotCommandHandler"]
