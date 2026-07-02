"""JSON-serializable snapshot builder for the Burry portfolio mirror dashboard.

Reads the tracker's SQLite event log plus the user's portfolio CSV and returns
a single, fully JSON-serializable ``BurrySnapshot`` -- the one data contract the
dashboard HTML template consumes via ``window.BURRY_DATA``.

Pure read: this module never writes the DB, the Sheet, or the CSV. It re-derives
the materialized state from SQLite (``replay_signals`` -> ``materialize_state``)
rather than reusing any pipeline in-memory state, which keeps ``read_snapshot``
independently runnable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from substack_trader.config import Config
from substack_trader.db import replay_signals
from substack_trader.portfolio_state import materialize_state
from substack_trader.rebalance import compute_rebalance
from substack_trader.user_portfolio import load_user_portfolio

SOURCE_LABEL = "signal_log.db"

# The committed sample portfolio used when no real user CSV exists. Overlaps
# Burry's tickers so the rebalance panel demonstrates every direction once the
# gold data (which carries weights + caps) is seeded.
SAMPLE_CSV = (
    Path(__file__).resolve().parents[1]
    / "dashboard/sample_user_portfolio.csv"
)


class BurrySnapshot(TypedDict):
    """The full JSON contract the dashboard template consumes."""

    generated_at: str
    as_of: str
    source: str
    positions: list[dict]
    caps: list[dict]
    signals: list[dict]
    rebalance: list[dict]
    rebalance_is_sample: bool
    stats: dict
    burry_allocation: dict
    user_allocation: dict


def _naive_utc(dt: datetime) -> datetime:
    """Normalize to naive UTC so aware/naive datetimes sort without TypeError."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _position_sort_key(pos: dict) -> tuple:
    """Open first, then weight_pct descending (None last), then ticker."""
    weight = pos["weight_pct"]
    return (
        0 if pos["status"] == "open" else 1,
        weight is None,
        -(weight if weight is not None else 0.0),
        pos["ticker"],
    )


def read_snapshot(config: Config) -> BurrySnapshot:
    """Build the dashboard snapshot from SQLite + the user portfolio.

    Pure read. Every datetime is emitted as an ISO string so ``json.dumps``
    (and the template's ``tojson`` filter) never sees a raw ``datetime``.
    """
    signals = replay_signals(config=config)
    state = materialize_state(signals)

    # Positions: one dict per PositionState, datetimes serialized to ISO. ------
    positions = [
        {
            "ticker": p.ticker,
            "instrument_type": p.instrument_type,
            "weight_pct": p.weight_pct,
            "target_pct": p.target_pct,
            "net_quantity": p.net_quantity,
            "conviction": p.conviction,
            "status": p.status,
            "last_confirmed_at": p.last_confirmed_at.isoformat(),
        }
        for p in state.positions.values()
    ]
    positions.sort(key=_position_sort_key)

    # Allocation slices for the Burry pie chart. Only open positions with a
    # disclosed weight become named slices; every open position whose size Burry
    # has not disclosed collapses into a single honest "Undisclosed" remainder,
    # so the chart never implies the disclosed names are the whole book.
    open_disclosed = [
        {"ticker": p["ticker"], "weight_pct": p["weight_pct"]}
        for p in positions
        if p["status"] == "open" and p["weight_pct"] is not None
    ]
    disclosed_total = round(sum(s["weight_pct"] for s in open_disclosed), 2)
    burry_allocation = {
        "slices": open_disclosed,  # already weight-descending from the sort above
        "disclosed_total_pct": disclosed_total,
        "undisclosed_pct": round(max(0.0, 100.0 - disclosed_total), 2),
        "undisclosed_count": sum(
            1
            for p in positions
            if p["status"] == "open" and p["weight_pct"] is None
        ),
    }

    # Aggregate caps: state.caps is dict[str, float] (grouping -> cap_pct). -----
    caps = [
        {"grouping": grouping, "cap_pct": cap_pct}
        for grouping, cap_pct in state.caps.items()
    ]
    caps.sort(key=lambda c: (-c["cap_pct"], c["grouping"]))

    # Signal timeline, most-recent valid_time first. --------------------------
    ordered_signals = sorted(
        signals, key=lambda s: _naive_utc(s.valid_time), reverse=True
    )
    signal_dicts = [s.model_dump(mode="json") for s in ordered_signals]

    # Rebalance with sample fallback. -----------------------------------------
    csv_path = config.user_portfolio_csv_path
    use_real = csv_path is not None and Path(csv_path).exists()
    rebalance_is_sample = not use_real
    chosen_csv = csv_path if use_real else SAMPLE_CSV
    user_portfolio = None
    try:
        user_portfolio = load_user_portfolio(chosen_csv)
        actions = compute_rebalance(
            state,
            user_portfolio,
            config.risk_multiplier,
            min_rebalance_usd=config.min_rebalance_usd,
            burry_signals=signals,
        )
    except (FileNotFoundError, ValueError):
        actions = []
    rebalance = [a.model_dump(mode="json") for a in actions]

    # Allocation slices for the "your portfolio" pie: each held ticker's share
    # of NAV by current dollar value (option rows collapse to their underlying
    # via value_by_ticker). Empty when the portfolio failed to load.
    user_slices: list[dict] = []
    user_nav = 0.0
    if user_portfolio is not None:
        nav = user_portfolio.nav
        user_nav = round(nav, 2)
        instrument = user_portfolio.instrument_by_ticker()
        for ticker, value in sorted(
            user_portfolio.value_by_ticker().items(),
            key=lambda kv: (-kv[1], kv[0]),
        ):
            user_slices.append(
                {
                    "ticker": ticker,
                    "value_usd": round(value, 2),
                    "value_pct": round(100.0 * value / nav, 2) if nav else 0.0,
                    "instrument_type": instrument.get(ticker, "other"),
                }
            )
    user_allocation = {
        "slices": user_slices,
        "nav": user_nav,
        "is_sample": rebalance_is_sample,
    }

    # Stats. ------------------------------------------------------------------
    open_positions = [p for p in positions if p["status"] == "open"]
    open_weights = [p["weight_pct"] for p in open_positions]
    stats = {
        "open_count": len(open_positions),
        "stale_count": sum(1 for p in open_positions if p["conviction"] == "stale"),
        "closed_count": sum(1 for p in positions if p["status"] == "closed"),
        # Skip None: open positions can carry weight_pct=None (confirmed by the
        # "None last" sort), so a bare sum() would raise TypeError.
        "modeled_weight_pct": round(sum(w for w in open_weights if w is not None), 2),
        "cap_count": len(caps),
        "signal_count": len(signal_dicts),
        "last_post_date": (
            _naive_utc(ordered_signals[0].valid_time).date().isoformat()
            if ordered_signals
            else None
        ),
    }

    snapshot: BurrySnapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": state.as_of.isoformat(),
        "source": SOURCE_LABEL,
        "positions": positions,
        "caps": caps,
        "signals": signal_dicts,
        "rebalance": rebalance,
        "rebalance_is_sample": rebalance_is_sample,
        "stats": stats,
        "burry_allocation": burry_allocation,
        "user_allocation": user_allocation,
    }

    # Fail loud if any raw datetime leaked; the template inlines this via tojson.
    json.dumps(snapshot)
    return snapshot
