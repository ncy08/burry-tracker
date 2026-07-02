"""Portfolio-state materialized view for the Burry mirror (Phase 4).

Replays the typed signal log (produced by `db.replay_signals`) into a per-ticker
model of Burry's modeled portfolio, then exposes that state to the Phase 4
constraint solver (`constraints.solve`) and the Phase 5 rebalance engine.

Design notes
------------
- **Full chronological replay, rebuilt from scratch each cycle**.
  The signal log is small, and full replay sidesteps the bitemporal correctness
  traps of incremental update (out-of-order arrival, late corrections,
  retroactive vetoes). `materialize_state` sorts its input defensively by
  (valid_time, transaction_time), so it is correct even when the caller does not
  pre-sort and replay is deterministic regardless of input order.
- **Filtering is this layer's job, not the data layer's**.
  `db.replay_signals` returns ALL rows. `materialize_state` drops FUTURE_PLAN
  (plan_status=pending) and CONDITIONAL (plan_status=conditional) signals before
  walking. HYPOTHETICAL and WATCHLIST remain in the walk but are no-ops.
- **Unknown weights stay None** (honest representation). The equal-weight
  default is the separate, opt-in `fill_unknown_weights` helper rather than
  fabricated inside the state walk, so an audit caller that wants honest unknowns
  and the solver caller that needs concrete numbers are both served.
- **Clock injection**. `materialize_state(now=...)` takes a zero-arg
  callable returning the current time, so the 90-day decay rule is testable
  without a freezegun dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from substack_trader.signals import InstrumentType, Signal

# A disclosed position degrades from "confirmed" to "stale" if Burry has not
# reaffirmed it within this window.
DECAY_WINDOW = timedelta(days=90)

# Signal types that are logged but never move portfolio state.
# Mirrors db.PENDING_TYPES | db.CONDITIONAL_TYPES; kept local so this module
# does not import the data layer just for a constant.
NON_STATE_MOVING = frozenset({"FUTURE_PLAN", "CONDITIONAL"})

Conviction = Literal["confirmed", "stale"]
PositionStatus = Literal["open", "closed"]


def _utcnow() -> datetime:
    """Naive UTC now.

    Matches the plan's `datetime.utcnow` intent without the Python 3.12+
    deprecation warning.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(dt: datetime) -> datetime:
    """Normalize to naive UTC.

    Signals round-tripped through SQLite may come back timezone-aware while the
    injected clock is naive (or vice versa). Normalizing both at the sort and
    decay boundaries keeps comparisons from raising aware/naive `TypeError`s.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class PositionState(BaseModel):
    """One ticker's modeled state after replaying the signal log."""

    ticker: str
    instrument_type: InstrumentType = "other"
    weight_pct: float | None = None  # modeled weight, in percent; None = unknown
    target_pct: float | None = None  # latest stated allocation target, in percent
    net_quantity: int = 0  # running buy-minus-sell tally (when quantities known)
    conviction: Conviction = "confirmed"
    status: PositionStatus = "open"
    last_confirmed_at: datetime


class BurryPortfolioState(BaseModel):
    """The full materialized view: positions plus aggregate caps."""

    as_of: datetime
    positions: dict[str, PositionState] = Field(default_factory=dict)
    caps: dict[str, float] = Field(default_factory=dict)  # grouping -> cap_pct

    def open_positions(self) -> dict[str, PositionState]:
        return {t: p for t, p in self.positions.items() if p.status == "open"}


def materialize_state(
    signals: list[Signal],
    *,
    now: Callable[[], datetime] = _utcnow,
) -> BurryPortfolioState:
    """Replay `signals` into a `BurryPortfolioState`.

    Applies the per-signal-type update rules. Pending and
    conditional signals are filtered out first; the remainder are sorted by
    `valid_time` (then `transaction_time`) and walked in order. After the walk,
    open positions unconfirmed for longer than `DECAY_WINDOW` degrade to
    conviction "stale".
    """
    state_signals = [s for s in signals if s.signal_type not in NON_STATE_MOVING]
    ordered = sorted(
        state_signals,
        key=lambda s: (_as_naive_utc(s.valid_time), _as_naive_utc(s.transaction_time)),
    )

    positions: dict[str, PositionState] = {}
    caps: dict[str, float] = {}
    targets: dict[str, float] = {}  # latest stated allocation target per ticker

    for sig in ordered:
        st = sig.signal_type

        if st == "ALLOCATION_TARGET":
            targets[sig.ticker] = sig.target_pct
            pos = positions.get(sig.ticker)
            if pos is not None:
                pos.target_pct = sig.target_pct
                if pos.weight_pct is None:
                    pos.weight_pct = sig.target_pct
            continue

        if st == "AGGREGATE_CAP":
            caps[sig.grouping] = sig.cap_pct
            continue

        if st in ("HYPOTHETICAL", "WATCHLIST"):
            continue  # log only

        # Remaining types are ticker-scoped position movers.
        ticker = sig.ticker
        if ticker is None:
            continue  # defensive: position movers are expected to carry a ticker
        pos = positions.get(ticker)

        if st == "EXECUTION":
            if sig.direction == "buy":
                if pos is None:
                    pos = PositionState(
                        ticker=ticker,
                        instrument_type=sig.instrument_type,
                        weight_pct=targets.get(ticker),
                        target_pct=targets.get(ticker),
                        last_confirmed_at=sig.valid_time,
                    )
                    positions[ticker] = pos
                else:
                    pos.instrument_type = sig.instrument_type
                    pos.status = "open"
                    if pos.weight_pct is None and ticker in targets:
                        pos.weight_pct = targets[ticker]
                pos.net_quantity += sig.quantity or 0
                pos.conviction = "confirmed"
                pos.last_confirmed_at = sig.valid_time
            else:  # sell
                if pos is not None:
                    pos.net_quantity -= sig.quantity or 0
                    pos.last_confirmed_at = sig.valid_time
                    pos.conviction = "confirmed"
                    # Only force a close when sizing is known and hits zero
                    #. Without quantities, a sell is a partial
                    # reduction; only a ClosureSignal force-closes.
                    if sig.quantity is not None and pos.net_quantity <= 0:
                        pos.status = "closed"

        elif st == "POSITION_DISCLOSURE":
            if pos is None:
                weight = sig.weight_hint if sig.weight_hint is not None else targets.get(ticker)
                pos = PositionState(
                    ticker=ticker,
                    instrument_type=sig.instrument_type,
                    weight_pct=weight,
                    target_pct=targets.get(ticker),
                    last_confirmed_at=sig.valid_time,
                )
                positions[ticker] = pos
            else:
                pos.instrument_type = sig.instrument_type
                pos.status = "open"
                if sig.weight_hint is not None:
                    pos.weight_pct = sig.weight_hint
                elif pos.weight_pct is None and ticker in targets:
                    pos.weight_pct = targets[ticker]
            pos.conviction = "confirmed"
            pos.last_confirmed_at = sig.valid_time

        elif st == "HOLD_CONFIRM":
            if pos is None:
                pos = PositionState(
                    ticker=ticker,
                    weight_pct=targets.get(ticker),
                    target_pct=targets.get(ticker),
                    last_confirmed_at=sig.valid_time,
                )
                positions[ticker] = pos
            else:
                pos.last_confirmed_at = sig.valid_time
            pos.conviction = "confirmed"
            pos.status = "open"  # confirm reopens a stale-but-not-closed name

        elif st == "CLOSURE":
            # A closure always marks the ticker closed, even if the
            # corresponding open is not (yet) in the log.
            if pos is None:
                pos = PositionState(ticker=ticker, last_confirmed_at=sig.valid_time)
                positions[ticker] = pos
            pos.status = "closed"
            pos.last_confirmed_at = sig.valid_time

    # Decay: open positions unconfirmed for longer than the window go "stale".
    as_of = now()
    age_from = _as_naive_utc(as_of)
    for pos in positions.values():
        if (
            pos.status == "open"
            and (age_from - _as_naive_utc(pos.last_confirmed_at)) > DECAY_WINDOW
        ):
            pos.conviction = "stale"

    return BurryPortfolioState(as_of=as_of, positions=positions, caps=caps)


def fill_unknown_weights(
    state: BurryPortfolioState,
    *,
    total_pct: float = 100.0,
) -> BurryPortfolioState:
    """Apply the equal-weight default to unknown-weight open positions.

    Open positions that still carry `weight_pct is None` split the residual
    budget (`total_pct` minus the sum of known open weights) equally. Because
    the split is equal across all unknowns, it is also equal within each
    instrument class, which satisfies the plan's "equal-weight across all
    unknown-weight positions in the same instrument class" without inventing a
    per-class budget the plan does not define.

    Returns a new state; the input is not mutated. Closed positions are ignored.
    """
    new_state = state.model_copy(deep=True)
    open_pos = [p for p in new_state.positions.values() if p.status == "open"]
    known = sum(p.weight_pct for p in open_pos if p.weight_pct is not None)
    unknown = [p for p in open_pos if p.weight_pct is None]
    residual = max(0.0, total_pct - known)
    if unknown and residual > 0:
        share = residual / len(unknown)
        for p in unknown:
            p.weight_pct = share
    elif unknown:
        # No residual budget left; assign zero so downstream never sees None.
        for p in unknown:
            p.weight_pct = 0.0

    return new_state
