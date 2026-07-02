"""Typed signal taxonomy for Burry post extraction.

Replaces the old single-class TradeLeg with ten discriminated Pydantic
models covering executions, position disclosures, holds, future plans,
conditional intents, closures, hypothetical mentions, watchlist items,
allocation targets, and aggregate caps. Each class declares an explicit
SCREAMING_SNAKE_CASE discriminator literal so the union resolves at parse
time.

Every signal carries a bitemporal pair on the shared base:
- valid_time:       when Burry took the action (per the post)
- transaction_time: when we ingested the post

The assertion_mode field implements the six-class taxonomy (PRESENT,
ABSENT, HYPOTHETICAL, CONDITIONAL, POSSIBLE, ASSOCIATED_WITH_OTHER). The
Stage one extractor sets it; the Stage two critic uses it as a veto input.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ConfidenceLevel = Literal["high", "medium", "low"]
InstrumentType = Literal["stock", "call", "put", "other"]
Direction = Literal["buy", "sell"]
AssertionMode = Literal[
    "PRESENT",
    "ABSENT",
    "HYPOTHETICAL",
    "CONDITIONAL",
    "POSSIBLE",
    "ASSOCIATED_WITH_OTHER",
]


class SignalBase(BaseModel):
    """Fields shared by every signal class.

    `ticker` is optional because AggregateCapSignal applies to a grouping
    (e.g., "puts") rather than a single ticker. All other signal classes
    are expected to populate it; the extractor and critic enforce that
    invariant rather than the schema.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str | None = None
    evidence_text: str
    confidence: ConfidenceLevel
    source_post_url: str
    valid_time: datetime
    transaction_time: datetime
    assertion_mode: AssertionMode = "PRESENT"


class ExecutionSignal(SignalBase):
    """Confirmed past trade. Direction + instrument + sizing + listing fields."""

    signal_type: Literal["EXECUTION"] = "EXECUTION"
    direction: Direction
    instrument_type: InstrumentType
    quantity: int | None = None
    strike: float | None = None
    expiration: str | None = None  # YYYY-MM-DD
    fill_price: float | None = None
    company_name: str | None = None  # full name when stated ("Meituan", "Haidilao")
    exchange: str | None = None  # listing venue: US, HK, ASX, or other
    currency: str | None = None  # price currency; "HKD 468" -> fill_price=468, currency=HKD


class PositionDisclosureSignal(SignalBase):
    """Holding disclosure without action. weight_hint stored as percent."""

    signal_type: Literal["POSITION_DISCLOSURE"] = "POSITION_DISCLOSURE"
    instrument_type: InstrumentType
    weight_hint: float | None = None  # e.g., 6.6 for "6.6% of portfolio"


class AllocationTargetSignal(SignalBase):
    """Stated target weight. target_pct stored as percent."""

    signal_type: Literal["ALLOCATION_TARGET"] = "ALLOCATION_TARGET"
    target_pct: float


class AggregateCapSignal(SignalBase):
    """Constraint over a group of positions. ticker is typically None."""

    signal_type: Literal["AGGREGATE_CAP"] = "AGGREGATE_CAP"
    grouping: str  # e.g., "puts", "options"
    cap_pct: float


class HoldConfirmSignal(SignalBase):
    """Explicit confirmation of an existing position. No new fields."""

    signal_type: Literal["HOLD_CONFIRM"] = "HOLD_CONFIRM"


class FuturePlanSignal(SignalBase):
    """Stated future action; logged but excluded from state replay."""

    signal_type: Literal["FUTURE_PLAN"] = "FUTURE_PLAN"
    timing_hint: str | None = None
    intent_summary: str


class ConditionalSignal(SignalBase):
    """Action contingent on event; logged but excluded from state replay."""

    signal_type: Literal["CONDITIONAL"] = "CONDITIONAL"
    condition_text: str
    intended_direction: Direction | None = None


class ClosureSignal(SignalBase):
    """Position exit. Marks the ticker closed in materialized state."""

    signal_type: Literal["CLOSURE"] = "CLOSURE"
    reason_hint: str | None = None


class HypotheticalSignal(SignalBase):
    """Speculative or analytical mention. Log only."""

    signal_type: Literal["HYPOTHETICAL"] = "HYPOTHETICAL"
    framing: str | None = None


class WatchlistSignal(SignalBase):
    """Monitoring without action. Log only."""

    signal_type: Literal["WATCHLIST"] = "WATCHLIST"


Signal = Annotated[
    ExecutionSignal | PositionDisclosureSignal | AllocationTargetSignal | AggregateCapSignal | HoldConfirmSignal | FuturePlanSignal | ConditionalSignal | ClosureSignal | HypotheticalSignal | WatchlistSignal,
    Field(discriminator="signal_type"),
]

SignalAdapter: TypeAdapter[Signal] = TypeAdapter(Signal)

# Discriminator literal -> model class. The Stage 1 parser uses this to drop
# fields the LLM attached that the resolved signal_type does not model: the flat
# Gemini response schema is a permissive superset (Gemini does not reliably
# support discriminated unions in `response_schema`), so the model can attach,
# e.g., option `quantity`/`expiration` to a POSITION_DISCLOSURE that only models
# `weight_hint`. See `substack_trader.extractor.parse_candidates`.
SIGNAL_CLASSES: dict[str, type[SignalBase]] = {
    cls.model_fields["signal_type"].default: cls
    for cls in (
        ExecutionSignal,
        PositionDisclosureSignal,
        AllocationTargetSignal,
        AggregateCapSignal,
        HoldConfirmSignal,
        FuturePlanSignal,
        ConditionalSignal,
        ClosureSignal,
        HypotheticalSignal,
        WatchlistSignal,
    )
}
