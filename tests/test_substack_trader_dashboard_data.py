"""Tests for the Burry dashboard snapshot builder (``dashboard_data.read_snapshot``).

These tests monkeypatch ``dashboard_data.replay_signals`` so they never touch a
real SQLite DB; the snapshot is built from a small synthetic signal set.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from substack_trader import dashboard_data
from substack_trader.config import Config
from substack_trader.signals import (
    AggregateCapSignal,
    ExecutionSignal,
    PositionDisclosureSignal,
    Signal,
)

_VALID = datetime(2026, 4, 10, 0, 0, 0)
_TXN = datetime(2026, 5, 6, 14, 8, 53)

_USER_CSV = "ticker,instrument_type,quantity,current_value_usd\nZZZ,stock,100,5000\n"


def _synthetic_signals() -> list[Signal]:
    """One EXECUTION buy (AAA) + one POSITION_DISCLOSURE (BBB, 10%) + one AGGREGATE_CAP."""
    return [
        ExecutionSignal(
            ticker="AAA",
            direction="buy",
            instrument_type="stock",
            evidence_text="bought AAA",
            confidence="high",
            source_post_url="https://example.com/1",
            valid_time=_VALID,
            transaction_time=_TXN,
        ),
        PositionDisclosureSignal(
            ticker="BBB",
            instrument_type="stock",
            weight_hint=10.0,
            evidence_text="holds about 10% in BBB",
            confidence="high",
            source_post_url="https://example.com/2",
            valid_time=_VALID,
            transaction_time=_TXN,
        ),
        AggregateCapSignal(
            grouping="puts",
            cap_pct=20.0,
            evidence_text="puts collectively capped at 20%",
            confidence="medium",
            source_post_url="https://example.com/3",
            valid_time=_VALID,
            transaction_time=_TXN,
        ),
    ]


@pytest.fixture
def patched_replay(monkeypatch):
    monkeypatch.setattr(
        dashboard_data, "replay_signals", lambda *a, **k: _synthetic_signals()
    )


def test_snapshot_shape_and_positions(patched_replay, tmp_path):
    """All documented keys present, JSON-serializable, both tickers, caps populated."""
    csv = tmp_path / "user.csv"
    csv.write_text(_USER_CSV, encoding="utf-8")
    cfg = Config()
    cfg.user_portfolio_csv_path = csv

    snap = dashboard_data.read_snapshot(cfg)

    expected_keys = {
        "generated_at",
        "as_of",
        "source",
        "positions",
        "caps",
        "signals",
        "rebalance",
        "rebalance_is_sample",
        "stats",
        "burry_allocation",
        "user_allocation",
    }
    assert set(snap) == expected_keys
    # JSON-serializable: no raw datetime leaked anywhere in the snapshot.
    json.dumps(snap)

    tickers = {p["ticker"] for p in snap["positions"]}
    assert {"AAA", "BBB"} <= tickers
    assert snap["stats"]["open_count"] == 2
    assert any(c["grouping"] == "puts" for c in snap["caps"])
    assert snap["source"] == "signal_log.db"
    assert snap["stats"]["signal_count"] == 3


def test_rebalance_sample_toggle_and_directions(patched_replay, tmp_path):
    """A real CSV sets rebalance_is_sample False and yields open_new + close_out."""
    csv = tmp_path / "user.csv"
    csv.write_text(_USER_CSV, encoding="utf-8")
    cfg = Config()
    cfg.user_portfolio_csv_path = csv

    snap = dashboard_data.read_snapshot(cfg)

    assert snap["rebalance_is_sample"] is False
    directions = {a["direction"] for a in snap["rebalance"]}
    # BBB: Burry holds (known weight), user does not -> open_new.
    assert "open_new" in directions
    # ZZZ: user holds, Burry does not -> close_out.
    assert "close_out" in directions


def test_rebalance_is_sample_when_no_user_csv(patched_replay):
    """With no user CSV configured, read_snapshot falls back to the committed sample."""
    cfg = Config()
    cfg.user_portfolio_csv_path = None

    snap = dashboard_data.read_snapshot(cfg)

    assert snap["rebalance_is_sample"] is True


def test_burry_allocation_disclosed_and_undisclosed(patched_replay, tmp_path):
    """Burry pie data: disclosed names slice out; the rest is one honest remainder."""
    csv = tmp_path / "user.csv"
    csv.write_text(_USER_CSV, encoding="utf-8")
    cfg = Config()
    cfg.user_portfolio_csv_path = csv

    ba = dashboard_data.read_snapshot(cfg)["burry_allocation"]

    # BBB carries a 10% disclosure; AAA (execution-only) has no disclosed size.
    assert ba["slices"] == [{"ticker": "BBB", "weight_pct": 10.0}]
    assert ba["disclosed_total_pct"] == 10.0
    assert ba["undisclosed_pct"] == 90.0
    assert ba["undisclosed_count"] == 1
    # Disclosed + undisclosed always reconcile to a full 100% pie.
    assert ba["disclosed_total_pct"] + ba["undisclosed_pct"] == 100.0


def test_user_allocation_weights_sum_to_100(patched_replay, tmp_path):
    """User pie data: each holding's share of NAV by current value, summing to 100%."""
    csv = tmp_path / "user.csv"
    csv.write_text(
        "ticker,instrument_type,quantity,current_value_usd\n"
        "AAA,stock,10,3000\nBBB,put,5,1000\n",
        encoding="utf-8",
    )
    cfg = Config()
    cfg.user_portfolio_csv_path = csv

    ua = dashboard_data.read_snapshot(cfg)["user_allocation"]

    assert ua["is_sample"] is False
    assert ua["nav"] == 4000.0
    # Sorted by value descending: AAA (75%) before BBB (25%).
    assert [s["ticker"] for s in ua["slices"]] == ["AAA", "BBB"]
    assert ua["slices"][0] == {
        "ticker": "AAA",
        "value_usd": 3000.0,
        "value_pct": 75.0,
        "instrument_type": "stock",
    }
    assert round(sum(s["value_pct"] for s in ua["slices"]), 2) == 100.0


def test_user_allocation_uses_sample_when_no_csv(patched_replay):
    """With no user CSV, the user pie mirrors the committed sample portfolio."""
    cfg = Config()
    cfg.user_portfolio_csv_path = None

    ua = dashboard_data.read_snapshot(cfg)["user_allocation"]

    assert ua["is_sample"] is True
    assert ua["slices"]  # the committed sample CSV is non-empty
    assert round(sum(s["value_pct"] for s in ua["slices"]), 2) == 100.0
