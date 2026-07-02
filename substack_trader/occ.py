"""OCC option symbol generator and Robinhood deep-link builder.

OCC option symbol format (industry standard, 21 chars):
    [Root][YYMMDD][C/P][Strike x 1000, zero-padded to 8]

Examples:
    QQQ Jan 15 2027 $550 put -> QQQ270115P00550000
    NVDA Jan 15 2027 $115 put -> NVDA270115P00115000

Robinhood publishes no deep-link URL that pre-fills a complete options
order. The OCC symbol is paste-resolvable in Robinhood's search bar; the
chain URL gets the user to the right contract list with one click.
"""

from __future__ import annotations

from datetime import date

from substack_trader.signals import ExecutionSignal


def occ_symbol(leg: ExecutionSignal) -> str | None:
    """Build the OCC option symbol for an options execution.

    Returns None for stock executions and for option executions missing
    strike or expiration. The to-open/to-close distinction the legacy
    TradeLeg carried via `action` is irrelevant for OCC symbol generation;
    the contract identifier is direction-agnostic.
    """
    if leg.instrument_type == "stock":
        return None
    if not (leg.expiration and leg.strike is not None and leg.ticker):
        return None
    exp = date.fromisoformat(leg.expiration)
    yymmdd = exp.strftime("%y%m%d")
    cp = "C" if leg.instrument_type == "call" else "P"
    strike_int = int(round(leg.strike * 1000))
    return f"{leg.ticker.upper()}{yymmdd}{cp}{strike_int:08d}"


def robinhood_links(leg: ExecutionSignal) -> dict[str, str]:
    """Return Robinhood URLs for an execution.

    For stocks: a single 'primary' link to the stock page.
    For options: 'chain' (the options chain) and 'stock' (underlying).
    The OCC symbol is the paste-resolvable contract identifier; pair it
    with the chain URL to get a near-one-click flow.
    """
    base = "https://robinhood.com"
    if leg.instrument_type == "stock":
        return {"primary": f"{base}/stocks/{leg.ticker}"}
    return {
        "chain": f"{base}/options/chains/{leg.ticker}",
        "stock": f"{base}/stocks/{leg.ticker}",
    }
