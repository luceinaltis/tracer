"""Tests for notification provider models and protocol."""

from datetime import datetime

from qracer.notifications.providers import (
    Notification,
    NotificationCategory,
    NotificationProvider,
)


class TestNotificationCategory:
    def test_all_categories_have_values(self):
        expected = {
            "price_alert",
            "portfolio_drawdown",
            "sector_concentration",
            "research_complete",
            "autonomous_mode",
            "daily_briefing",
        }
        assert {c.value for c in NotificationCategory} == expected


class TestNotification:
    def test_creation_with_defaults(self):
        n = Notification(
            category=NotificationCategory.PRICE_ALERT,
            title="AAPL above $200",
            body="AAPL reached $201.50",
        )
        assert n.category == NotificationCategory.PRICE_ALERT
        assert n.title == "AAPL above $200"
        assert n.body == "AAPL reached $201.50"
        assert isinstance(n.created_at, datetime)
        assert n.metadata == {}

    def test_creation_with_metadata(self):
        n = Notification(
            category=NotificationCategory.PORTFOLIO_DRAWDOWN,
            title="Drawdown alert",
            body="Portfolio down 12%",
            metadata={"drawdown_pct": 12.0, "ticker": "TSLA"},
        )
        assert n.metadata["drawdown_pct"] == 12.0

    def test_frozen(self):
        n = Notification(
            category=NotificationCategory.RESEARCH_COMPLETE,
            title="Done",
            body="Research finished",
        )
        try:
            n.title = "Changed"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestNotificationProviderProtocol:
    def test_fake_satisfies_protocol(self):
        class _Fake:
            async def send(self, notification: Notification) -> bool:
                return True

        assert isinstance(_Fake(), NotificationProvider)

    def test_missing_method_fails_protocol(self):
        class _Bad:
            pass

        assert not isinstance(_Bad(), NotificationProvider)
