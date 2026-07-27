"""Notification layer protocols and models.

Defines the NotificationProvider protocol and the Notification data model
that all notification channel adapters must support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class NotificationCategory(Enum):
    """Categories of notifications the system can emit."""

    PRICE_ALERT = "price_alert"
    PORTFOLIO_DRAWDOWN = "portfolio_drawdown"
    SECTOR_CONCENTRATION = "sector_concentration"
    RESEARCH_COMPLETE = "research_complete"
    AUTONOMOUS_MODE = "autonomous_mode"
    DAILY_BRIEFING = "daily_briefing"


@dataclass(frozen=True)
class Notification:
    """A notification to be delivered via one or more channels."""

    category: NotificationCategory
    title: str
    body: str
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class NotificationProvider(Protocol):
    """Capability: deliver notifications to an external channel."""

    async def send(self, notification: Notification) -> bool:
        """Send a notification. Returns True on success, False on failure."""
        ...
