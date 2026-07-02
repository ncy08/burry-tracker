"""User-portfolio diff and rebalance engine for the Burry mirror (Phase 5).

Takes the constrained Burry target portfolio (Phase 4) and the user's own
holdings (Phase 5 CSV reader), and computes the dollar-denominated rebalance
actions that move the user toward Burry's allocation, scaled by a risk
multiplier.

Design decisions
----------------
1. **Honest unknowns, never a false close-out**.
   `solve` only returns weights for open positions whose `weight_pct` is known,
   so a Burry position with an undisclosed size is absent from the solved
   targets. We must NOT read that absence as "Burry exited," or a name the user
   holds would be wrongly flagged close-out. The authoritative set of
   Burry-held tickers is `burry_state.open_positions()`, kept separate from the
   solved dollar targets. Unknown-weight Burry holdings therefore produce no
   dollar recommendation (we will not fabricate a size) but also never a sell.
   Set `fill_unknowns=True` to apply the equal-weight default first and get
   a concrete target for every open name; the default is honest-unknown.

2. **Options collapse to ticker level in v1**. The engine does
   not match by (strike, expiration); it diffs one combined dollar position per
   ticker. When the ticker is an option on either side, the action carries a
   Notes hint to review the contract spec manually. If `burry_signals` is
   supplied, the hint includes Burry's latest disclosed strike/expiration.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from substack_trader.constraints import solve
from substack_trader.portfolio_state import (
    BurryPortfolioState,
    fill_unknown_weights,
)
from substack_trader.signals import ExecutionSignal, Signal
from substack_trader.user_portfolio import UserPortfolio

RebalanceDirection = Literal["buy", "sell", "open_new", "close_out"]

_OPTION_TYPES = frozenset({"call", "put"})


class RebalanceAction(BaseModel):
    """One recommended move: a signed dollar delta on a single ticker."""

    ticker: str
    direction: RebalanceDirection
    delta_usd: float  # signed: positive = buy/add, negative = sell/trim
    target_pct: float | None = None  # risk-scaled Burry target weight (percent)
    current_value_usd: float = 0.0  # user's current dollar value in this ticker
    notes: str = ""


def _latest_execution_contracts(
    signals: list[Signal],
) -> dict[str, ExecutionSignal]:
    """Latest ExecutionSignal carrying a strike, per ticker (by valid_time)."""
    latest: dict[str, ExecutionSignal] = {}
    for sig in signals:
        if not isinstance(sig, ExecutionSignal) or sig.ticker is None:
            continue
        if sig.strike is None:
            continue
        prev = latest.get(sig.ticker)
        if prev is None or sig.valid_time >= prev.valid_time:
            latest[sig.ticker] = sig
    return latest


def _options_note(
    ticker: str, contracts: dict[str, ExecutionSignal]
) -> str:
    """Build the contract-review hint, enriched if a contract is known."""
    spec = contracts.get(ticker)
    if spec is not None:
        parts = []
        if spec.strike is not None:
            suffix = "p" if spec.instrument_type == "put" else "c"
            parts.append(f"{spec.strike:g}{suffix}")
        if spec.expiration:
            parts.append(f"exp {spec.expiration}")
        detail = " ".join(parts)
        return (
            "Options position: review existing contract vs. Burry's spec"
            + (f" ({detail})" if detail else "")
            + ". Contract selection is a manual call."
        )
    return (
        "Options position: review existing contract vs. Burry's contract spec. "
        "Contract selection is a manual call."
    )


def compute_rebalance(
    burry_state: BurryPortfolioState,
    user_portfolio: UserPortfolio,
    risk_multiplier: float = 1.0,
    *,
    min_rebalance_usd: float = 100.0,
    fill_unknowns: bool = False,
    burry_signals: list[Signal] | None = None,
) -> list[RebalanceAction]:
    """Compute rebalance actions moving the user toward the Burry target.

    For each ticker in the union of Burry's open book and the user's holdings:
    the risk-scaled Burry target weight becomes a target dollar amount against
    the user's NAV; the signed delta from the user's current value is the
    rebalance. Deltas below `min_rebalance_usd` are dropped.

    Tickers Burry does not hold at all become close-outs scaled to the user's
    whole position. Tickers Burry holds but the user does not become
    open-news. See the module docstring for the unknown-weight and options
    handling.
    """
    state = fill_unknown_weights(burry_state) if fill_unknowns else burry_state
    target_weights = solve(state, state.caps)  # percent, known-weight open names

    burry_open = burry_state.open_positions()
    burry_open_tickers = set(burry_open)
    burry_instrument = {t: p.instrument_type for t, p in burry_open.items()}

    nav = user_portfolio.nav
    user_values = user_portfolio.value_by_ticker()
    user_instrument = user_portfolio.instrument_by_ticker()
    contracts = (
        _latest_execution_contracts(burry_signals) if burry_signals else {}
    )

    def is_options(ticker: str) -> bool:
        return (
            burry_instrument.get(ticker) in _OPTION_TYPES
            or user_instrument.get(ticker) in _OPTION_TYPES
        )

    actions: list[RebalanceAction] = []
    universe = set(target_weights) | set(user_values) | burry_open_tickers

    for ticker in sorted(universe):
        current = user_values.get(ticker, 0.0)
        in_user = ticker in user_values
        target_wt = target_weights.get(ticker)  # None = unknown or not-Burry

        if target_wt is None:
            # No solved dollar target. Either Burry does not hold this ticker
            # (close it out of the user's book), or Burry holds it with an
            # undisclosed size (honest unknown: recommend nothing).
            if ticker in burry_open_tickers:
                continue
            if not in_user:
                continue
            delta = -current  # liquidate the whole user position
            if abs(delta) < min_rebalance_usd:
                continue
            note = _options_note(ticker, contracts) if is_options(ticker) else ""
            actions.append(
                RebalanceAction(
                    ticker=ticker,
                    direction="close_out",
                    delta_usd=round(delta, 2),
                    target_pct=None,
                    current_value_usd=round(current, 2),
                    notes=note,
                )
            )
            continue

        scaled_wt = target_wt * risk_multiplier
        target_dollars = (scaled_wt / 100.0) * nav
        delta = target_dollars - current
        if abs(delta) < min_rebalance_usd:
            continue

        if not in_user:
            direction: RebalanceDirection = "open_new"
        elif delta > 0:
            direction = "buy"
        else:
            direction = "sell"

        note = _options_note(ticker, contracts) if is_options(ticker) else ""
        actions.append(
            RebalanceAction(
                ticker=ticker,
                direction=direction,
                delta_usd=round(delta, 2),
                target_pct=round(scaled_wt, 4),
                current_value_usd=round(current, 2),
                notes=note,
            )
        )

    return actions
