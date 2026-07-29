"""Server — long-running service loop for qracer serve.

Drives TaskExecutor and AlertMonitor without user input, sending
notifications via the NotificationRegistry when events occur.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from qracer.agent_monitor import AgentMonitor
from qracer.alert_monitor import AlertMonitor
from qracer.autonomous import AutonomousMonitor
from qracer.briefing import BriefingScheduler
from qracer.notifications.bot_commands import BotCommandHandler
from qracer.notifications.providers import Notification, NotificationCategory
from qracer.notifications.registry import NotificationRegistry
from qracer.notifications.telegram_poller import BotCommand, TelegramBotPoller
from qracer.task_executor import TaskExecutor

logger = logging.getLogger(__name__)

# A monitor paired with the coroutine that handles one round of its results.
_MonitorEntry = tuple[Any, Callable[[Any], Awaitable[None]]]


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
        agent_monitor: AgentMonitor | None = None,
        briefing_scheduler: "BriefingScheduler | None" = None,
        telegram_poller: TelegramBotPoller | None = None,
        tick_interval: float = 1.0,
    ) -> None:
        self._alert_monitor = alert_monitor
        self._task_executor = task_executor
        self._autonomous_monitor = autonomous_monitor
        self._agent_monitor = agent_monitor
        self._briefing_scheduler = briefing_scheduler
        self._notifications = notifications or NotificationRegistry()
        self._telegram_poller = telegram_poller
        self._tick_interval = tick_interval
        self._shutdown_event = asyncio.Event()
        self._started_at: float | None = None

        # Each monitor paired with the coroutine handling one round of its results.
        # Adding a monitor means one entry here — the tick loop stays untouched.
        self._monitors: list[_MonitorEntry] = [
            (alert_monitor, self._handle_alert_results),
            (task_executor, self._handle_task_results),
        ]
        if autonomous_monitor is not None:
            self._monitors.append((autonomous_monitor, self._handle_autonomous_results))
        if agent_monitor is not None:
            self._monitors.append((agent_monitor, self._handle_agent_results))
        if briefing_scheduler is not None:
            self._monitors.append((briefing_scheduler, self._handle_briefing_result))

        # Inbound Telegram bot commands are a separate concern from the tick loop.
        self._bot = BotCommandHandler(alert_monitor.store, task_executor.store, self._status_text)

    async def run(self) -> None:
        """Main loop — runs until shutdown() is called."""
        logger.info("Server started (tick=%.1fs)", self._tick_interval)
        self._started_at = time.monotonic()

        while not self._shutdown_event.is_set():
            await self._tick()
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._tick_interval,
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Server stopped")

    async def _tick(self) -> None:
        """Single heartbeat — poll every due monitor, then inbound bot commands."""
        for monitor, handle in self._monitors:
            if not monitor.should_check():
                continue
            try:
                results = await monitor.check()
            except Exception:
                logger.debug("%s check failed", type(monitor).__name__, exc_info=True)
                continue
            await handle(results)

        if self._telegram_poller is not None:
            try:
                commands = await self._telegram_poller.poll()
            except Exception:
                logger.debug("Telegram poll failed", exc_info=True)
                commands = []
            for command in commands:
                await self._handle_bot_command(command)

    # -- per-monitor result handlers ------------------------------------

    async def _handle_alert_results(self, triggered: list) -> None:
        for result in triggered:
            logger.info("Alert triggered: %s", result.message)
            await self._notify(NotificationCategory.PRICE_ALERT, result.message, result.message)

    async def _handle_task_results(self, results: list) -> None:
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

    async def _handle_autonomous_results(self, auto_alerts: list) -> None:
        for alert in auto_alerts:
            logger.info("Autonomous alert: %s", alert.summary)
            await self._notify(
                NotificationCategory.AUTONOMOUS_MODE,
                f"[{alert.severity.value.upper()}] {alert.ticker}",
                alert.summary,
            )

    async def _handle_agent_results(self, agent_results: list) -> None:
        for ar in agent_results:
            if ar.ok:
                logger.info("Agent ran: %s [%s]", ar.name, ar.model)
            else:
                logger.warning("Agent failed: %s — %s", ar.name, ar.error)

    async def _handle_briefing_result(self, sent: str | None) -> None:
        if sent:
            logger.info("Daily briefing pushed")

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
        """Route a bot command to a reply (delegates to the BotCommandHandler)."""
        return self._bot.dispatch(command)

    def _status_text(self) -> str:
        """The ``/status`` reply — server state the bot handler can't see itself."""
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
