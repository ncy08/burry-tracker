"""User portfolio CSV reader for the Burry mirror (Phase 5).

The user's own holdings live in a CSV at a fixed path that the pipeline
re-reads every cycle. This module parses that CSV into
a `UserPortfolio` and exposes the aggregates the rebalance engine needs: total
NAV and per-ticker dollar value.

CSV schema (header row required)::

    ticker,instrument_type,quantity,current_value_usd
    AAPL,stock,100,18500.00
    GEO,stock,500,9000.00

`instrument_type` is normalized to the same `InstrumentType` literal the signal
taxonomy uses (`stock`, `call`, `put`, `other`); anything unrecognized becomes
`other` rather than raising, since a hand-maintained CSV should never hard-fail
the pipeline over a typo in a non-numeric column.
"""

from __future__ import annotations

import csv
from pathlib import Path

from pydantic import BaseModel

from substack_trader.signals import InstrumentType

REQUIRED_COLUMNS = frozenset(
    {"ticker", "instrument_type", "quantity", "current_value_usd"}
)

_KNOWN_INSTRUMENTS: frozenset[str] = frozenset({"stock", "call", "put", "other"})
_OPTION_TYPES: frozenset[str] = frozenset({"call", "put"})


def _normalize_instrument(raw: str) -> InstrumentType:
    """Map a free-text CSV value to a known InstrumentType, defaulting to other."""
    key = (raw or "").strip().lower()
    if key in _KNOWN_INSTRUMENTS:
        return key  # type: ignore[return-value]
    if key in ("equity", "equities", "shares", "common"):
        return "stock"
    return "other"


class UserPosition(BaseModel):
    """One row of the user's portfolio CSV."""

    ticker: str
    instrument_type: InstrumentType = "other"
    quantity: float = 0.0
    current_value_usd: float = 0.0


class UserPortfolio(BaseModel):
    """The user's holdings, parsed from CSV.

    Positions are kept as a flat list so the same ticker can appear more than
    once (e.g., stock plus options on the same underlying). The rebalance engine
    collapses to ticker-level dollar deltas in version one, so
    `value_by_ticker` and `instrument_by_ticker` do that aggregation here.
    """

    positions: list[UserPosition] = []

    @property
    def nav(self) -> float:
        """Total net asset value: sum of every position's current dollar value."""
        return sum(p.current_value_usd for p in self.positions)

    @property
    def tickers(self) -> set[str]:
        return {p.ticker for p in self.positions}

    def value_by_ticker(self) -> dict[str, float]:
        """Current dollar value per ticker, summed across instrument rows."""
        out: dict[str, float] = {}
        for p in self.positions:
            out[p.ticker] = out.get(p.ticker, 0.0) + p.current_value_usd
        return out

    def instrument_by_ticker(self) -> dict[str, InstrumentType]:
        """Representative instrument type per ticker.

        If any row for a ticker is an option, the ticker reports as an option
        (the engine surfaces a contract-review note for options); otherwise it
        reports the first row's type.
        """
        out: dict[str, InstrumentType] = {}
        for p in self.positions:
            existing = out.get(p.ticker)
            if existing in _OPTION_TYPES:
                continue
            if existing is None or p.instrument_type in _OPTION_TYPES:
                out[p.ticker] = p.instrument_type
        return out


def load_user_portfolio(path: str | Path) -> UserPortfolio:
    """Read the user portfolio CSV at `path` into a `UserPortfolio`.

    Raises `FileNotFoundError` if the file is missing (a stale or absent file
    should be loud) and `ValueError` if the header is
    missing a required column. Blank lines and rows with an empty ticker are
    skipped. Non-numeric quantity / value cells default to 0.0 rather than
    aborting the whole read.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"User portfolio CSV not found at {csv_path}")

    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        header = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - header
        if missing:
            raise ValueError(
                f"User portfolio CSV at {csv_path} is missing columns: "
                f"{', '.join(sorted(missing))}"
            )

        positions: list[UserPosition] = []
        for row in reader:
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            positions.append(
                UserPosition(
                    ticker=ticker,
                    instrument_type=_normalize_instrument(row.get("instrument_type", "")),
                    quantity=_to_float(row.get("quantity")),
                    current_value_usd=_to_float(row.get("current_value_usd")),
                )
            )

    return UserPortfolio(positions=positions)


def _to_float(raw: str | None) -> float:
    """Coerce a CSV cell to float, treating blank/garbage as 0.0."""
    if raw is None:
        return 0.0
    cleaned = raw.strip().replace(",", "").replace("$", "")
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0
