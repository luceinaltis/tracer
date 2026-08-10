"""TelegramBotPoller — receive bot commands from Telegram via long-polling.

Companion to :mod:`qracer.notifications.telegram_adapter` (which only sends).
This module adds inbound command receiving so users can interact with the
qracer service remotely from their phone.

Uses only the stdlib ``urllib`` so there is no extra dependency, mirroring
the existing TelegramAdapter design.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"

# Telegram caps a single message at 4096 characters; leave a small margin so
# truncation suffixes still fit.
_DEFAULT_MESSAGE_CHAR_LIMIT = 4000


@dataclass(frozen=True)
class BotMessage:
    """A free-text (non-command) message from a Telegram chat."""

    text: str


@dataclass(frozen=True)
class BotCommand:
    """A parsed bot command from a Telegram message.

    Example::

        BotCommand.parse("/analyze AAPL")
        # → BotCommand(action="analyze", args=["AAPL"], raw_text="/analyze AAPL")
    """

    action: str
    args: list[str]
    raw_text: str

    @classmethod
    def parse(cls, text: str) -> BotCommand | None:
        """Parse a Telegram message into a :class:`BotCommand`.

        Returns ``None`` if the text is not a recognised command (i.e. does
        not start with ``/`` or contains no action token).

        Telegram bot commands may include a ``@botname`` suffix when sent in
        groups (e.g. ``/analyze@qracerbot AAPL``); the suffix is stripped so
        the action is just ``analyze``.
        """
        text = (text or "").strip()
        if not text or not text.startswith("/"):
            return None
        parts = text[1:].split()
        if not parts:
            return None
        action = parts[0].split("@", 1)[0].lower()
        if not action:
            return None
        return cls(action=action, args=parts[1:], raw_text=text)


class TelegramBotPoller:
    """Receive bot commands from Telegram via the ``getUpdates`` long-poll API.

    Tracks the update offset so messages are never returned twice, filters
    messages to those originating from the authorised chat, and parses
    incoming text into :class:`BotCommand` objects.

    Replies can be sent back to the same chat via :meth:`send_reply`.

    Usage::

        poller = TelegramBotPoller(bot_token="...", chat_id="123")
        commands = await poller.poll()
        for cmd in commands:
            await poller.send_reply(f"Got: {cmd.action}")
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: int = 30,
        message_char_limit: int = _DEFAULT_MESSAGE_CHAR_LIMIT,
    ) -> None:
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required but was empty")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is required but was empty")
        self._bot_token = bot_token
        self._chat_id = str(chat_id)
        self._timeout = max(0, int(timeout))
        self._message_char_limit = message_char_limit
        self._offset: int | None = None

    @property
    def offset(self) -> int | None:
        """Current update offset (``None`` until the first update arrives)."""
        return self._offset

    @property
    def chat_id(self) -> str:
        """The authorised chat ID this poller filters by."""
        return self._chat_id

    async def poll(self) -> list[BotCommand]:
        """Long-poll Telegram for new commands.

        Returns a list of :class:`BotCommand` parsed from messages that
        arrived from the authorised chat. The offset is advanced past
        the highest update ID returned, so subsequent calls only return
        new messages.

        Network and API errors are logged and converted to an empty list
        — the caller is expected to retry on the next tick.
        """
        commands, _ = await asyncio.to_thread(self._fetch_updates)
        return commands

    async def poll_all(self) -> tuple[list[BotCommand], list[BotMessage]]:
        """Long-poll Telegram for both commands and free-text messages.

        Like :meth:`poll` but also returns non-command text messages so
        the caller can route them to a conversation engine.
        """
        return await asyncio.to_thread(self._fetch_updates)

    def _fetch_updates(self) -> tuple[list[BotCommand], list[BotMessage]]:
        url = f"{_TELEGRAM_API}/bot{self._bot_token}/getUpdates"
        params: dict[str, Any] = {
            "timeout": self._timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset is not None:
            params["offset"] = self._offset
        full_url = f"{url}?{urllib.parse.urlencode(params)}"

        try:
            with urllib.request.urlopen(full_url, timeout=self._timeout + 5) as resp:
                if resp.status != 200:
                    logger.warning("Telegram getUpdates returned status %s", resp.status)
                    return [], []
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            logger.error("Telegram getUpdates failed (HTTP %s): %s", exc.code, exc.reason)
            return [], []
        except urllib.error.URLError as exc:
            logger.error("Telegram getUpdates failed (network): %s", exc.reason)
            return [], []
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("Telegram getUpdates returned invalid JSON: %s", exc)
            return [], []

        if not isinstance(payload, dict) or not payload.get("ok"):
            logger.warning(
                "Telegram getUpdates returned ok=false: %s",
                payload.get("description") if isinstance(payload, dict) else payload,
            )
            return [], []

        commands: list[BotCommand] = []
        messages: list[BotMessage] = []
        max_update_id = -1
        for update in payload.get("result", []):
            update_id = update.get("update_id")
            if isinstance(update_id, int) and update_id > max_update_id:
                max_update_id = update_id

            message = update.get("message")
            if not isinstance(message, dict):
                continue

            chat = message.get("chat") or {}
            if str(chat.get("id")) != self._chat_id:
                logger.debug("Ignoring message from unauthorised chat %s", chat.get("id"))
                continue

            text = message.get("text")
            if not isinstance(text, str):
                continue

            cmd = BotCommand.parse(text)
            if cmd is not None:
                commands.append(cmd)
            elif text.strip():
                messages.append(BotMessage(text=text.strip()))

        if max_update_id >= 0:
            self._offset = max_update_id + 1

        return commands, messages

    async def send_reply(self, text: str) -> bool:
        """Send a plain-text reply to the authorised chat.

        Long replies are truncated to ``message_char_limit`` characters
        with a trailing ``"..."``. Returns ``True`` on HTTP 200.
        """
        return await asyncio.to_thread(self._send_reply_sync, text)

    def _send_reply_sync(self, text: str) -> bool:
        if not text:
            return False
        if len(text) > self._message_char_limit:
            text = text[: self._message_char_limit - 3] + "..."

        url = f"{_TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = resp.status == 200
                if not ok:
                    logger.warning("Telegram sendMessage returned status %s", resp.status)
                return ok
        except urllib.error.HTTPError as exc:
            logger.error("Telegram sendMessage failed (HTTP %s): %s", exc.code, exc.reason)
            return False
        except urllib.error.URLError as exc:
            logger.error("Telegram sendMessage failed (network): %s", exc.reason)
            return False
