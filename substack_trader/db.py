"""Bitemporal SQLite event log for Burry signals.

Provides `get_connection`, `init_db`, and `migrate_db`, plus two
domain-specific functions: `insert_signal` and `replay_signals`.

Schema:
- `signal_log` stores every typed Signal with both a `valid_time` (when Burry
  acted) and `transaction_time` (when we ingested), so audit questions like
  "what did the system know on date X" can be answered separately from
  "what was actually true on date X".
- `signal_json` carries the parsed Signal as JSON for round-trip dispatch
  through `SignalAdapter`.
- `raw_llm_output` carries the full Stage 1 LLM payload so future schema
  changes can re-parse old captures without re-running extraction.
- `plan_status` distinguishes signals that move portfolio state
  (`confirmed`) from FuturePlan (`pending`) and Conditional (`conditional`)
  signals. Derived at insert time from the signal's discriminator.

`replay_signals` returns ALL rows in chronological order. Phase 4's
`materialize_state` is the layer that filters on `plan_status='confirmed'`
when it walks signals to rebuild portfolio state.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from substack_trader.config import Config
from substack_trader.signals import Signal, SignalAdapter

DB_FILENAME = "signal_log.db"

PENDING_TYPES = {"FUTURE_PLAN"}
CONDITIONAL_TYPES = {"CONDITIONAL"}

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS signal_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_type       TEXT NOT NULL,
  ticker            TEXT,
  valid_time        TIMESTAMP NOT NULL,
  transaction_time  TIMESTAMP NOT NULL,
  post_url          TEXT NOT NULL,
  evidence_text     TEXT NOT NULL,
  confidence        TEXT NOT NULL,
  stage1_rationale  TEXT,
  stage2_decision   TEXT,
  stage2_reason     TEXT,
  signal_json       TEXT NOT NULL,
  raw_llm_output    TEXT,
  plan_status       TEXT NOT NULL DEFAULT 'confirmed'
);
CREATE INDEX IF NOT EXISTS idx_signal_log_ticker ON signal_log(ticker);
CREATE INDEX IF NOT EXISTS idx_signal_log_valid_time ON signal_log(valid_time);
CREATE INDEX IF NOT EXISTS idx_signal_log_post_url ON signal_log(post_url);
CREATE INDEX IF NOT EXISTS idx_signal_log_plan_status ON signal_log(plan_status);
"""


def _db_path(config: Config | None) -> str:
    cfg = config if config is not None else Config()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return str(cfg.state_dir / DB_FILENAME)


def get_connection(config: Config | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and standard PRAGMAs."""
    conn = sqlite3.connect(_db_path(config))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(config: Config | None = None) -> None:
    """Create the signal_log table and indexes if they don't exist."""
    conn = get_connection(config)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def migrate_db(config: Config | None = None) -> None:
    """Apply schema migrations. Idempotent; safe to call repeatedly.

    Phase 3 ships the initial schema only, so this currently re-runs the
    base script. Future column additions follow the
    standard additive pattern: PRAGMA table_info, append
    new columns with ALTER TABLE behind an existence check.
    """
    init_db(config)


def _derive_plan_status(signal_type: str) -> str:
    if signal_type in PENDING_TYPES:
        return "pending"
    if signal_type in CONDITIONAL_TYPES:
        return "conditional"
    return "confirmed"


def insert_signal(
    signal: Signal,
    *,
    stage1_rationale: str | None = None,
    stage2_decision: str | None = None,
    stage2_reason: str | None = None,
    raw_llm_output: str | None = None,
    config: Config | None = None,
) -> int:
    """Persist a Signal plus its Stage 1/2 audit payload. Returns the row id.

    `plan_status` is derived from the signal's discriminator and is NOT a
    caller-overridable parameter, so a FuturePlanSignal cannot be silently
    written as `confirmed` (and vice versa).
    """
    plan_status = _derive_plan_status(signal.signal_type)
    conn = get_connection(config)
    try:
        cursor = conn.execute(
            """
            INSERT INTO signal_log (
                signal_type, ticker, valid_time, transaction_time,
                post_url, evidence_text, confidence,
                stage1_rationale, stage2_decision, stage2_reason,
                signal_json, raw_llm_output, plan_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.signal_type,
                signal.ticker,
                signal.valid_time.isoformat(),
                signal.transaction_time.isoformat(),
                signal.source_post_url,
                signal.evidence_text,
                signal.confidence,
                stage1_rationale,
                stage2_decision,
                stage2_reason,
                signal.model_dump_json(),
                raw_llm_output,
                plan_status,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def replay_signals(
    since: datetime | None = None,
    *,
    config: Config | None = None,
) -> list[Signal]:
    """Return all logged signals in chronological order.

    Order: `valid_time` ASC, with `transaction_time` ASC as the tiebreaker.
    `id` ASC is the final tiebreaker so the order is fully deterministic.

    `since` filters by `valid_time >= since`. Pass `None` (default) for the
    full log. Phase 4's `materialize_state` is responsible for filtering on
    `plan_status='confirmed'`; this function does not.
    """
    conn = get_connection(config)
    try:
        if since is None:
            cursor = conn.execute(
                "SELECT signal_json FROM signal_log "
                "ORDER BY valid_time ASC, transaction_time ASC, id ASC"
            )
        else:
            cursor = conn.execute(
                "SELECT signal_json FROM signal_log "
                "WHERE valid_time >= ? "
                "ORDER BY valid_time ASC, transaction_time ASC, id ASC",
                (since.isoformat(),),
            )
        return [SignalAdapter.validate_json(row["signal_json"]) for row in cursor.fetchall()]
    finally:
        conn.close()
