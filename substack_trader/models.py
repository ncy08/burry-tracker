"""Data models for the substack trader pipeline.

`Signal` and the ten typed signal classes live in `substack_trader.signals`.
This module retains the post container (`BurryPost`) and the rebalance-summary
notification payload. The legacy `TradeLeg` dataclass was removed when the
typed-signal taxonomy landed in Phase 1 of the portfolio-mirror redesign.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from substack_trader.signals import Signal


@dataclass
class BurryPost:
    gmail_message_id: str
    post_url: str
    title: str
    pub_date: datetime
    body_text: str
    legs: list[Signal] = field(default_factory=list)


@dataclass
class RebalanceActionSummary:
    """Lightweight summary of a single rebalance recommendation for the notifier."""

    ticker: str
    delta_dollars: float


@dataclass
class NotificationPayload:
    """Payload for the post-cycle rebalance notification.

    Reshaped in Phase 1 to support the new portfolio-mirror flow. The
    notifier formatter renders a coherent rebalance summary across Gmail
    draft, macOS banner, and Twilio SMS channels using these fields.
    """

    sheet_url: str
    rebalance_count: int
    top_actions: list[RebalanceActionSummary] = field(default_factory=list)
    total_delta_usd: float = 0.0
