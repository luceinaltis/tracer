"""Server — long-running service loop for qracer serve.

Drives TaskExecutor and AlertMonitor without user input, sending
notifications via the NotificationRegistry when events occur.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from qracer.agent_monitor import AgentMonitor
from qracer.alert_monitor import AlertMonitor
from qracer.autonomous import AutonomousMonitor
from qracer.briefing import BriefingScheduler
from qracer.data.providers import StreamingProvider
from qracer.notifications.bot_commands import BotCommandHandler
from qracer.notifications.providers import Notification, NotificationCategory
from qracer.notifications.registry import NotificationRegistry
from qracer.notifications.telegram_poller import BotCommand, BotMessage, TelegramBotPoller
from qracer.task_executor import TaskExecutor

if TYPE_CHECKING:
    from qracer.conversation.engine import ConversationEngine

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
        streaming_adapter: StreamingProvider | None = None,
        conversation_engine: "ConversationEngine | None" = None,
        tick_interval: float = 1.0,
    ) -> None:
        self._alert_monitor = alert_monitor
        self._task_executor = task_executor
        self._autonomous_monitor = autonomous_monitor
        self._agent_monitor = agent_monitor
        self._briefing_scheduler = briefing_scheduler
        self._notifications = notifications or NotificationRegistry()
        self._telegram_poller = telegram_poller
        self._streaming_adapter = streaming_adapter
        self._conversation_engine = conversation_engine
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
        self._bot = BotCommandHandler(
            alert_monitor.store,
            task_executor.store,
            self._status_text,
            conversation_enabled=conversation_engine is not None,
        )

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
                if self._conversation_engine is not None:
                    commands, messages = await self._telegram_poller.poll_all()
                else:
                    commands = await self._telegram_poller.poll()
                    messages = []
            except Exception:
                logger.debug("Telegram poll failed", exc_info=True)
                commands = []
                messages = []
            for command in commands:
                await self._handle_bot_command(command)
            for message in messages:
                await self._handle_bot_message(message)

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

    async def _handle_bot_message(self, message: BotMessage) -> None:
        """Route a free-text Telegram message through ConversationEngine."""
        if self._conversation_engine is None or self._telegram_poller is None:
            return
        text = message.text
        if not text:
            return
        await self._telegram_poller.send_reply("Analyzing your query...")
        try:
            response = await self._conversation_engine.query(text)
            await self._telegram_poller.send_reply(response.text)
        except Exception as exc:
            logger.exception("Conversation query failed: %s", text[:80])
            await self._telegram_poller.send_reply(f"Query failed: {exc}")

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
