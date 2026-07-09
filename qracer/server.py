"""Server — long-running service loop for qracer serve.

Drives TaskExecutor and AlertMonitor without user input, sending
notifications via the NotificationRegistry when events occur.
"""

from __future__ import annotations

import asyncio
import logging
import time

from qracer.alert_monitor import AlertMonitor
from qracer.alerts import AlertCondition
from qracer.autonomous import AutonomousMonitor
from qracer.data.providers import StreamingProvider
from qracer.notifications.providers import Notification, NotificationCategory
from qracer.notifications.registry import NotificationRegistry
from qracer.notifications.telegram_poller import BotCommand, TelegramBotPoller
from qracer.task_executor import TaskExecutor
from qracer.tasks import TaskActionType

logger = logging.getLogger(__name__)


class Server:
    """Headless service loop — replaces the REPL's input()-driven heartbeat.

    Usage::

        server = Server(alert_monitor, task_executor, notifications)
        await server.run()       # blocks until shutdown() is called
        server.shutdown()        # from a signal handler
    """

    def __init__(
        self,
        alert_monitor: AlertMonitor,
        task_executor: TaskExecutor,
        notifications: NotificationRegistry | None = None,
        *,
        autonomous_monitor: AutonomousMonitor | None = None,
        telegram_poller: TelegramBotPoller | None = None,
        streaming_adapter: StreamingProvider | None = None,
        tick_interval: float = 1.0,
    ) -> None:
        self._alert_monitor = alert_monitor
        self._task_executor = task_executor
        self._autonomous_monitor = autonomous_monitor
        self._notifications = notifications or NotificationRegistry()
        self._telegram_poller = telegram_poller
        self._streaming_adapter = streaming_adapter
        self._tick_interval = tick_interval
        self._shutdown_event = asyncio.Event()
        self._started_at: float | None = None

    async def run(self) -> None:
        """Main loop — runs until shutdown() is called."""
        logger.info("Server started (tick=%.1fs)", self._tick_interval)
        self._started_at = time.monotonic()

        await self._start_streaming()

        try:
            while not self._shutdown_event.is_set():
                await self._tick()
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._tick_interval,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_streaming()

        logger.info("Server stopped")

    async def _start_streaming(self) -> None:
        """Connect the streaming adapter and wire it to the alert monitor.

        On any failure the server falls back to REST polling — the
        adapter is simply set to ``None`` so the tick loop keeps working.
        """
        if self._streaming_adapter is None:
            return
        try:
            await self._streaming_adapter.connect()
        except Exception:
            logger.warning(
                "Streaming adapter failed to connect — falling back to REST polling",
                exc_info=True,
            )
            self._streaming_adapter = None
            return

        self._streaming_adapter.on_price(self._on_stream_price)

        # Subscribe to every ticker that currently has an active alert.
        tickers = sorted({a.ticker for a in self._alert_monitor.store.get_active()})
        if tickers:
            try:
                await self._streaming_adapter.subscribe(tickers)
            except Exception:
                logger.warning("Streaming subscribe failed for %s", tickers, exc_info=True)
        logger.info("Streaming adapter wired (subscribed=%d)", len(tickers))

    async def _stop_streaming(self) -> None:
        """Disconnect the streaming adapter on shutdown."""
        if self._streaming_adapter is None:
            return
        try:
            await self._streaming_adapter.disconnect()
        except Exception:
            logger.debug("Streaming adapter disconnect failed", exc_info=True)

    async def _on_stream_price(self, ticker: str, price: float) -> None:
        """Evaluate alerts for *ticker* immediately on a real-time price."""
        try:
            triggered = self._alert_monitor.evaluate_price(ticker, price)
        except Exception:
            logger.debug("Streaming alert evaluation failed", exc_info=True)
            return
        for result in triggered:
            logger.info("Alert triggered (stream): %s", result.message)
            await self._notify(
                NotificationCategory.PRICE_ALERT,
                result.message,
                result.message,
            )

    async def _tick(self) -> None:
        """Single heartbeat — check alerts and tasks."""
        if self._alert_monitor.should_check():
            try:
                triggered = await self._alert_monitor.check()
                for result in triggered:
                    logger.info("Alert triggered: %s", result.message)
                    await self._notify(
                        NotificationCategory.PRICE_ALERT,
                        result.message,
                        result.message,
                    )
            except Exception:
                logger.debug("Alert check failed", exc_info=True)

        if self._task_executor.should_check():
            try:
                results = await self._task_executor.check()
                for r in results:
                    if r.success:
                        logger.info("Task completed: %s", r.task.describe())
                    else:
                        logger.warning("Task failed: %s — %s", r.task.describe(), r.error)
                        await self._notify(
                            NotificationCategory.AUTONOMOUS_MODE,
                            f"Task failed: {r.task.describe()}",
                            r.error or "unknown error",
                        )
            except Exception:
                logger.debug("Task check failed", exc_info=True)

        if self._autonomous_monitor and self._autonomous_monitor.should_check():
            try:
                auto_alerts = await self._autonomous_monitor.check()
                for alert in auto_alerts:
                    logger.info("Autonomous alert: %s", alert.summary)
                    await self._notify(
                        NotificationCategory.AUTONOMOUS_MODE,
                        f"[{alert.severity.value.upper()}] {alert.ticker}",
                        alert.summary,
                    )
            except Exception:
                logger.debug("Autonomous check failed", exc_info=True)

        if self._telegram_poller is not None:
            try:
                commands = await self._telegram_poller.poll()
            except Exception:
                logger.debug("Telegram poll failed", exc_info=True)
                commands = []
            for command in commands:
                await self._handle_bot_command(command)

    async def _handle_bot_command(self, command: BotCommand) -> None:
        """Dispatch an incoming bot command and reply with the result."""
        try:
            reply = self._dispatch_bot_command(command)
        except Exception as exc:
            logger.exception("Bot command handler failed: /%s", command.action)
            reply = f"Error handling /{command.action}: {exc}"
        if reply and self._telegram_poller is not None:
            await self._telegram_poller.send_reply(reply)

    def _dispatch_bot_command(self, command: BotCommand) -> str:
        """Route a :class:`BotCommand` to the matching handler.

        Handlers return the reply text to send back to the user. Long
        replies are truncated by the poller before transmission.
        """
        action = command.action
        if action in {"help", "start"}:
            return self._cmd_help()
        if action == "status":
            return self._cmd_status()
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

    # ------------------------------------------------------------------
    # Individual command handlers
    # ------------------------------------------------------------------

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

    def _cmd_status(self) -> str:
        uptime = "unknown"
        if self._started_at is not None:
            uptime = _format_duration(time.monotonic() - self._started_at)
        channels = ", ".join(self._notifications.channels) or "none"
        autonomous = "on" if self._autonomous_monitor else "off"
        return (
            "qracer status\n"
            f"  uptime: {uptime}\n"
            f"  notifications: {channels}\n"
            f"  autonomous: {autonomous}"
        )

    def _cmd_alerts(self) -> str:
        alerts = self._alert_monitor.store.get_active()
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
        alert = self._alert_monitor.store.create(ticker, condition, threshold)
        return f"Created alert {alert.id}: {alert.describe()}"

    def _cmd_tasks(self) -> str:
        tasks = self._task_executor.store.get_active()
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
            task = self._task_executor.store.create(action_type, {"ticker": ticker}, schedule_spec)
        except ValueError as exc:
            return f"Invalid schedule: {exc}"
        return f"Scheduled task {task.id}: {task.describe()}"

    async def _notify(self, category: NotificationCategory, title: str, body: str) -> None:
        """Send a notification if any channels are registered."""
        if not self._notifications.channels:
            return
        notification = Notification(category=category, title=title, body=body)
        await self._notifications.notify(notification)

    def shutdown(self) -> None:
        """Signal the server to stop after the current tick."""
        self._shutdown_event.set()


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as ``"1h 23m 45s"`` (omitting empty units)."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)
