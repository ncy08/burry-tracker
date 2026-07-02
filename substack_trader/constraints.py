"""Aggregate-cap constraint solver for the Burry mirror (Phase 4).

Given a materialized `BurryPortfolioState` (modeled target weights per ticker)
and a set of aggregate caps (e.g. "all puts <= 5%"), `solve` returns adjusted
per-ticker weights that stay as close to Burry's stated weights as possible
while never letting any capped group exceed its cap.

Why a deviation-minimizing objective, not max-Sharpe
----------------------------------------------------
This is a *mirroring* system, so there are no return forecasts to optimize.
PyPortfolioOpt is the chosen solver: we use its constrained-optimization
machinery (`EfficientFrontier` +
`add_sector_constraints`) with a custom convex objective that minimizes squared
deviation from the modeled target weights. The optimizer pulls a capped group
down to its cap and redistributes the freed weight to the remaining positions in
the way that stays closest to Burry's stated allocation. `expected_returns` and
`cov_matrix` are required by the `EfficientFrontier` constructor to define the
asset universe and problem size, but the deviation objective never references
them, so we pass a zero mean vector and an identity covariance as structural
placeholders.

Units
-----
All weights and caps are in percent (matching `weight_hint`, `target_pct`, and
`cap_pct` elsewhere in the taxonomy). Internally the optimizer works in
fractions that sum to 1.0; `solve` normalizes the modeled targets to a
fully-invested 100% portfolio, interprets each `cap_pct` as a fraction of that
100%, and scales the solved weights back to percent on the way out.
"""

from __future__ import annotations

import cvxpy as cp
import numpy as np
import pandas as pd
from pypfopt import EfficientFrontier

from substack_trader.portfolio_state import BurryPortfolioState

# Maps a free-text AggregateCapSignal grouping to the instrument_type values it
# constrains. Singular and plural forms both resolve; "options" spans two types.
_GROUPING_ALIASES: dict[str, frozenset[str]] = {
    "put": frozenset({"put"}),
    "puts": frozenset({"put"}),
    "call": frozenset({"call"}),
    "calls": frozenset({"call"}),
    "option": frozenset({"call", "put"}),
    "options": frozenset({"call", "put"}),
    "derivative": frozenset({"call", "put"}),
    "derivatives": frozenset({"call", "put"}),
    "stock": frozenset({"stock"}),
    "stocks": frozenset({"stock"}),
    "equity": frozenset({"stock"}),
    "equities": frozenset({"stock"}),
}


def _grouping_to_instruments(grouping: str) -> frozenset[str]:
    key = grouping.strip().lower()
    # Fall back to treating the grouping as a literal instrument_type so a cap
    # on an exact type (e.g. "other") still binds.
    return _GROUPING_ALIASES.get(key, frozenset({key}))


def solve(
    state: BurryPortfolioState,
    caps: dict[str, float] | None = None,
) -> dict[str, float]:
    """Return capped per-ticker weights (percent) for the open, known-weight book.

    Only open positions with a known `weight_pct` enter the optimization; you
    cannot cap-optimize a weight you do not know. Run `fill_unknown_weights`
    first if you want unknown-weight positions included. `caps` defaults to the
    caps already on the state. With no binding caps, the normalized targets pass
    through unchanged.
    """
    caps = state.caps if caps is None else caps

    positions = [
        p
        for p in state.positions.values()
        if p.status == "open" and p.weight_pct is not None
    ]
    if not positions:
        return {}

    tickers = [p.ticker for p in positions]
    target_pct = np.array([float(p.weight_pct) for p in positions], dtype=float)
    total = target_pct.sum()
    if total <= 0:
        return {}
    target = target_pct / total  # normalize to a fully-invested book (sum == 1)

    if not caps:
        return {t: round(float(w) * 100.0, 6) for t, w in zip(tickers, target)}

    mu = pd.Series(np.zeros(len(tickers)), index=tickers)
    cov = pd.DataFrame(np.eye(len(tickers)), index=tickers, columns=tickers)
    ef = EfficientFrontier(mu, cov, weight_bounds=(0, 1))

    sector_mapper = {p.ticker: p.instrument_type for p in positions}
    sector_upper: dict[str, float] = {}

    for grouping, cap_pct in caps.items():
        instruments = _grouping_to_instruments(grouping)
        cap_frac = cap_pct / 100.0
        if len(instruments) == 1:
            itype = next(iter(instruments))
            # Keep the tightest cap if two groupings target the same type.
            sector_upper[itype] = min(sector_upper.get(itype, cap_frac), cap_frac)
        else:
            # A multi-instrument grouping (e.g. "options" = calls + puts) cannot
            # be one sector in add_sector_constraints, so cap the union directly.
            mask = np.array(
                [1.0 if p.instrument_type in instruments else 0.0 for p in positions]
            )
            if mask.sum() > 0:
                ef.add_constraint(lambda w, m=mask, c=cap_frac: (m @ w) <= c)

    if sector_upper:
        ef.add_sector_constraints(
            sector_mapper, sector_lower={}, sector_upper=sector_upper
        )

    ef.convex_objective(
        lambda w: cp.sum_squares(w - target), weights_sum_to_one=True
    )
    weights = ef.clean_weights()
    return {t: round(float(weights[t]) * 100.0, 6) for t in tickers}
