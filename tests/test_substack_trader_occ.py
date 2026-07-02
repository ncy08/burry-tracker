"""OCC option symbol generator tests.

Cases drawn from the April 23 fixture post: four put options on QQQ x2,
NVDA, and SOXX. Verifies the canonical 21-char OCC format and that
stocks plus incomplete legs return None.

`occ_symbol` and `robinhood_links` operate on `ExecutionSignal` since the
Phase 1 taxonomy migration replaced the old `TradeLeg`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from substack_trader.occ import occ_symbol, robinhood_links
from substack_trader.signals import ExecutionSignal

_T = datetime(2026, 4, 23, tzinfo=timezone.utc)


def _opt(ticker: str, strike: float | None, exp: str | None, kind: str = "put") -> ExecutionSignal:
    return ExecutionSignal(
        ticker=ticker,
        direction="buy",
        instrument_type=kind,  # type: ignore[arg-type]
        strike=strike,
        expiration=exp,
        evidence_text="test",
        confidence="high",
        source_post_url="https://michaeljburry.substack.com/p/test",
        valid_time=_T,
        transaction_time=_T,
    )


def _stock(ticker: str) -> ExecutionSignal:
    return ExecutionSignal(
        ticker=ticker,
        direction="buy",
        instrument_type="stock",
        evidence_text="test",
        confidence="high",
        source_post_url="https://michaeljburry.substack.com/p/test",
        valid_time=_T,
        transaction_time=_T,
    )


def test_occ_qqq_jan_2027_550_put() -> None:
    assert occ_symbol(_opt("QQQ", 550.0, "2027-01-15", "put")) == "QQQ270115P00550000"


def test_occ_qqq_mar_2027_525_put() -> None:
    assert occ_symbol(_opt("QQQ", 525.0, "2027-03-19", "put")) == "QQQ270319P00525000"


def test_occ_nvda_jan_2027_115_put() -> None:
    assert occ_symbol(_opt("NVDA", 115.0, "2027-01-15", "put")) == "NVDA270115P00115000"


def test_occ_soxx_jan_2027_330_put() -> None:
    assert occ_symbol(_opt("SOXX", 330.0, "2027-01-15", "put")) == "SOXX270115P00330000"


def test_occ_call_uses_C_marker() -> None:
    assert occ_symbol(_opt("AAPL", 200.0, "2026-12-18", "call")) == "AAPL261218C00200000"


def test_occ_lowercase_ticker_uppercased() -> None:
    assert occ_symbol(_opt("qqq", 550.0, "2027-01-15", "put")) == "QQQ270115P00550000"


def test_occ_fractional_strike_rounded() -> None:
    # $192.50 -> 19250000 (strike x 1000, zero-padded to 8)
    assert occ_symbol(_opt("AAPL", 192.5, "2026-12-18", "call")) == "AAPL261218C00192500"


def test_occ_stock_returns_none() -> None:
    assert occ_symbol(_stock("GME")) is None


def test_occ_option_with_null_strike_returns_none() -> None:
    assert occ_symbol(_opt("QQQ", None, "2027-01-15", "put")) is None


def test_occ_option_with_null_expiration_returns_none() -> None:
    assert occ_symbol(_opt("QQQ", 550.0, None, "put")) is None


def test_robinhood_links_stock_returns_primary_only() -> None:
    links = robinhood_links(_stock("GME"))
    assert links == {"primary": "https://robinhood.com/stocks/GME"}


def test_robinhood_links_option_returns_chain_and_stock() -> None:
    links = robinhood_links(_opt("QQQ", 550.0, "2027-01-15", "put"))
    assert links == {
        "chain": "https://robinhood.com/options/chains/QQQ",
        "stock": "https://robinhood.com/stocks/QQQ",
    }
