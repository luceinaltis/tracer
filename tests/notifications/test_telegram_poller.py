"""Tests for TelegramBotPoller and BotCommand."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from qracer.notifications.telegram_poller import (
    BotCommand,
    BotMessage,
    TelegramBotPoller,
)

# ---------------------------------------------------------------------------
# BotCommand.parse
# ---------------------------------------------------------------------------


class TestBotCommandParse:
    def test_simple_command(self) -> None:
        cmd = BotCommand.parse("/status")
        assert cmd is not None
        assert cmd.action == "status"
        assert cmd.args == []
        assert cmd.raw_text == "/status"

    def test_command_with_args(self) -> None:
        cmd = BotCommand.parse("/analyze AAPL")
        assert cmd is not None
        assert cmd.action == "analyze"
        assert cmd.args == ["AAPL"]

    def test_command_with_multiple_args(self) -> None:
        cmd = BotCommand.parse("/alert AAPL above 200")
        assert cmd is not None
        assert cmd.action == "alert"
        assert cmd.args == ["AAPL", "above", "200"]

    def test_command_with_botname_suffix(self) -> None:
        cmd = BotCommand.parse("/analyze@qracerbot AAPL")
        assert cmd is not None
        assert cmd.action == "analyze"
        assert cmd.args == ["AAPL"]

    def test_action_lowercased(self) -> None:
        cmd = BotCommand.parse("/STATUS")
        assert cmd is not None
        assert cmd.action == "status"

    def test_leading_trailing_whitespace(self) -> None:
        cmd = BotCommand.parse("   /tasks   ")
        assert cmd is not None
        assert cmd.action == "tasks"

    def test_non_command_returns_none(self) -> None:
        assert BotCommand.parse("hello world") is None

    def test_empty_string_returns_none(self) -> None:
        assert BotCommand.parse("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert BotCommand.parse("   ") is None

    def test_lone_slash_returns_none(self) -> None:
        assert BotCommand.parse("/") is None

    def test_only_botname_after_slash_returns_none(self) -> None:
        assert BotCommand.parse("/@bot") is None


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestTelegramBotPollerInit:
    def test_requires_bot_token(self) -> None:
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            TelegramBotPoller(bot_token="", chat_id="12345")

    def test_requires_chat_id(self) -> None:
        with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
            TelegramBotPoller(bot_token="tok", chat_id="")

    def test_chat_id_coerced_to_str(self) -> None:
        poller = TelegramBotPoller(bot_token="tok", chat_id=12345)  # type: ignore[arg-type]
        assert poller.chat_id == "12345"

    def test_initial_offset_is_none(self) -> None:
        poller = TelegramBotPoller(bot_token="tok", chat_id="1")
        assert poller.offset is None


# ---------------------------------------------------------------------------
# poll() — mocked HTTP
# ---------------------------------------------------------------------------


def _mock_response(payload: dict) -> MagicMock:
    """Build a context-manager mock that ``urlopen`` returns."""
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_update(update_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


class TestTelegramBotPollerPoll:
    @pytest.fixture()
    def poller(self) -> TelegramBotPoller:
        return TelegramBotPoller(bot_token="tok", chat_id="999", timeout=1)

    async def test_poll_parses_commands_and_advances_offset(
        self, poller: TelegramBotPoller
    ) -> None:
        payload = {
            "ok": True,
            "result": [
                _make_update(10, 999, "/status"),
                _make_update(11, 999, "/analyze AAPL"),
            ],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            commands = await poller.poll()

        assert len(commands) == 2
        assert commands[0].action == "status"
        assert commands[1].action == "analyze"
        assert commands[1].args == ["AAPL"]
        assert poller.offset == 12  # max update_id + 1

    async def test_poll_filters_unauthorised_chats(self, poller: TelegramBotPoller) -> None:
        payload = {
            "ok": True,
            "result": [
                _make_update(1, 999, "/status"),
                _make_update(2, 1234, "/leak"),  # different chat — must be ignored
                _make_update(3, 999, "/tasks"),
            ],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            commands = await poller.poll()

        actions = [c.action for c in commands]
        assert actions == ["status", "tasks"]
        # Offset still advances past all updates so we don't re-fetch them.
        assert poller.offset == 4

    async def test_poll_skips_non_text_messages(self, poller: TelegramBotPoller) -> None:
        payload = {
            "ok": True,
            "result": [
                {
                    "update_id": 5,
                    "message": {
                        "message_id": 5,
                        "chat": {"id": 999, "type": "private"},
                        "photo": [{"file_id": "abc"}],
                    },
                },
                _make_update(6, 999, "/status"),
            ],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            commands = await poller.poll()

        assert len(commands) == 1
        assert commands[0].action == "status"
        assert poller.offset == 7

    async def test_poll_skips_non_command_text(self, poller: TelegramBotPoller) -> None:
        payload = {
            "ok": True,
            "result": [_make_update(1, 999, "hello there")],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            commands = await poller.poll()

        assert commands == []
        assert poller.offset == 2  # update_id still consumed

    async def test_poll_passes_offset_on_subsequent_calls(self, poller: TelegramBotPoller) -> None:
        payload1 = {"ok": True, "result": [_make_update(20, 999, "/status")]}
        payload2 = {"ok": True, "result": [_make_update(21, 999, "/tasks")]}

        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        responses = [_mock_response(payload1), _mock_response(payload2)]
        with patch(target, side_effect=responses) as mock:
            await poller.poll()
            await poller.poll()

        # First call has no offset, second call must include offset=21.
        first_url = mock.call_args_list[0][0][0]
        second_url = mock.call_args_list[1][0][0]
        assert "offset=" not in first_url
        assert "offset=21" in second_url

    async def test_poll_returns_empty_on_http_error(self, poller: TelegramBotPoller) -> None:
        exc = urllib.error.HTTPError(
            url="https://api.telegram.org",
            code=500,
            msg="Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, side_effect=exc):
            commands = await poller.poll()

        assert commands == []
        assert poller.offset is None

    async def test_poll_returns_empty_on_url_error(self, poller: TelegramBotPoller) -> None:
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, side_effect=urllib.error.URLError("Connection refused")):
            commands = await poller.poll()
        assert commands == []

    async def test_poll_returns_empty_on_invalid_json(self, poller: TelegramBotPoller) -> None:
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b"<not json>"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=resp):
            commands = await poller.poll()
        assert commands == []

    async def test_poll_returns_empty_on_ok_false(self, poller: TelegramBotPoller) -> None:
        payload = {"ok": False, "description": "bad token"}
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            commands = await poller.poll()
        assert commands == []

    async def test_poll_uses_token_in_url(self, poller: TelegramBotPoller) -> None:
        payload = {"ok": True, "result": []}
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)) as mock:
            await poller.poll()
        url = mock.call_args[0][0]
        assert "/bottok/getUpdates" in url


# ---------------------------------------------------------------------------
# send_reply — mocked HTTP
# ---------------------------------------------------------------------------


class TestTelegramBotPollerReply:
    @pytest.fixture()
    def poller(self) -> TelegramBotPoller:
        return TelegramBotPoller(
            bot_token="tok",
            chat_id="999",
            timeout=1,
            message_char_limit=50,
        )

    async def test_send_reply_success(self, poller: TelegramBotPoller) -> None:
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)

        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=resp) as mock:
            ok = await poller.send_reply("hello")

        assert ok is True
        req = mock.call_args[0][0]
        assert "/bottok/sendMessage" in req.full_url
        body = json.loads(req.data)
        assert body == {"chat_id": "999", "text": "hello"}

    async def test_send_reply_truncates_long_text(self, poller: TelegramBotPoller) -> None:
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)

        long_text = "x" * 200
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=resp) as mock:
            await poller.send_reply(long_text)

        body = json.loads(mock.call_args[0][0].data)
        assert len(body["text"]) == 50  # message_char_limit
        assert body["text"].endswith("...")

    async def test_send_reply_empty_text_returns_false(self, poller: TelegramBotPoller) -> None:
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target) as mock:
            ok = await poller.send_reply("")
        assert ok is False
        mock.assert_not_called()

    async def test_send_reply_http_error_returns_false(self, poller: TelegramBotPoller) -> None:
        exc = urllib.error.HTTPError(
            url="https://api.telegram.org",
            code=403,
            msg="Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, side_effect=exc):
            ok = await poller.send_reply("hello")
        assert ok is False

    async def test_send_reply_url_error_returns_false(self, poller: TelegramBotPoller) -> None:
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, side_effect=urllib.error.URLError("offline")):
            ok = await poller.send_reply("hello")
        assert ok is False


# ---------------------------------------------------------------------------
# BotMessage
# ---------------------------------------------------------------------------


class TestBotMessage:
    def test_frozen(self) -> None:
        msg = BotMessage(text="hello")
        assert msg.text == "hello"

    def test_equality(self) -> None:
        assert BotMessage(text="hi") == BotMessage(text="hi")
        assert BotMessage(text="hi") != BotMessage(text="bye")


# ---------------------------------------------------------------------------
# poll_all — commands + free-text messages
# ---------------------------------------------------------------------------


class TestTelegramBotPollerPollAll:
    @pytest.fixture()
    def poller(self) -> TelegramBotPoller:
        return TelegramBotPoller(bot_token="tok", chat_id="999", timeout=1)

    async def test_poll_all_returns_commands_and_messages(self, poller: TelegramBotPoller) -> None:
        payload = {
            "ok": True,
            "result": [
                _make_update(10, 999, "/status"),
                _make_update(11, 999, "What is the outlook for AAPL?"),
                _make_update(12, 999, "/alerts"),
            ],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            commands, messages = await poller.poll_all()

        assert len(commands) == 2
        assert commands[0].action == "status"
        assert commands[1].action == "alerts"
        assert len(messages) == 1
        assert messages[0].text == "What is the outlook for AAPL?"
        assert poller.offset == 13

    async def test_poll_all_strips_whitespace_from_messages(
        self, poller: TelegramBotPoller
    ) -> None:
        payload = {
            "ok": True,
            "result": [_make_update(1, 999, "  hello  ")],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            _, messages = await poller.poll_all()

        assert len(messages) == 1
        assert messages[0].text == "hello"

    async def test_poll_all_skips_whitespace_only_messages(self, poller: TelegramBotPoller) -> None:
        payload = {
            "ok": True,
            "result": [_make_update(1, 999, "   ")],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            _, messages = await poller.poll_all()

        assert messages == []

    async def test_poll_all_filters_unauthorised_chats(self, poller: TelegramBotPoller) -> None:
        payload = {
            "ok": True,
            "result": [
                _make_update(1, 999, "legit question"),
                _make_update(2, 1234, "sneaky message"),
            ],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            _, messages = await poller.poll_all()

        assert len(messages) == 1
        assert messages[0].text == "legit question"

    async def test_poll_all_empty_on_error(self, poller: TelegramBotPoller) -> None:
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, side_effect=urllib.error.URLError("offline")):
            commands, messages = await poller.poll_all()

        assert commands == []
        assert messages == []

    async def test_poll_still_returns_only_commands(self, poller: TelegramBotPoller) -> None:
        payload = {
            "ok": True,
            "result": [
                _make_update(10, 999, "/status"),
                _make_update(11, 999, "free text query"),
            ],
        }
        target = "qracer.notifications.telegram_poller.urllib.request.urlopen"
        with patch(target, return_value=_mock_response(payload)):
            commands = await poller.poll()

        assert len(commands) == 1
        assert commands[0].action == "status"
