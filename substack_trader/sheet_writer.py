"""Five-tab Google Sheet writer for the Burry portfolio mirror (Phase 6).

Replaces the old three-tab schema (Pending, History, Positions) with five tabs
purpose-built for the portfolio-mirror flow:

- **SignalLog** — every extracted signal with full provenance. Append-only with
  a composite dedup gate so re-running backfill never double-writes a row.
- **BurryPortfolio** — the current modeled state per open ticker, refreshed in
  full from the materialized `BurryPortfolioState` each cycle.
- **AggregateConstraints** — the parent caps over groups of positions.
- **Rebalance** — the dollar-denominated actions the user executes, with a
  Status dropdown (Pending / Executed / Skipped).
- **AuditTrail** — the per-signal critic decision record for explainability.

Bootstrap behavior
----------------------------------
`bootstrap_sheets` creates only the tabs that do not already exist by name, so a
tab the user has hand-edited (reordered columns, changed dropdown values, added
rows) is left untouched on re-run. The three legacy tabs are renamed to
`*_archived_2026_05_03` and left in place for the user's reference rather than
migrated programmatically.

Dedup gate
--------------------------
`append_signals` reads the existing SignalLog once at the start of the call,
builds a set of `(post_url, ticker, signal_type, valid_time)` tuples, and writes
only the rows whose key is unseen. The valid-time column is written with the RAW
input option so the ISO timestamp round-trips byte-for-byte, which keeps the
dedup key stable across backfill replays (USER_ENTERED would coerce the string
into a serial date number and break the match).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from substack_trader.portfolio_state import BurryPortfolioState
from substack_trader.rebalance import RebalanceAction
from substack_trader.signals import AggregateCapSignal, Signal

if TYPE_CHECKING:
    import gspread

logger = logging.getLogger(__name__)


# --- Tab names -------------------------------------------------------------

SIGNAL_LOG_TAB = "SignalLog"
BURRY_PORTFOLIO_TAB = "BurryPortfolio"
AGGREGATE_CONSTRAINTS_TAB = "AggregateConstraints"
REBALANCE_TAB = "Rebalance"
AUDIT_TRAIL_TAB = "AuditTrail"

# Legacy three-tab schema, renamed (not migrated) on first bootstrap.
_ARCHIVE_SUFFIX = "_archived_2026_05_03"
_LEGACY_TABS = ("Pending", "History", "Positions")


# --- Headers ---------------------------------------------------------------

SIGNAL_LOG_HEADERS = [
    "Detected At",
    "Post Date",
    "Post URL",
    "Signal Type",
    "Ticker",
    "Company Name",
    "Exchange",
    "Currency",
    "Direction",
    "Instrument Type",
    "Quantity",
    "Strike",
    "Expiration",
    "Weight Hint",
    "Confidence",
    "Stage 1 Rationale",
    "Stage 2 Decision",
    "Stage 2 Reason",
    "Evidence Quote",
    "Source",
]

BURRY_PORTFOLIO_HEADERS = [
    "Ticker",
    "Instrument Type",
    "Current Weight Pct",
    "Target Weight Pct",
    "Conviction",
    "First Seen",
    "Last Confirmed",
    "Last Signal Type",
    "Latest Evidence",
]

AGGREGATE_CONSTRAINTS_HEADERS = [
    "Grouping",
    "Cap Pct",
    "Source Signal Date",
    "Evidence Quote",
]

REBALANCE_HEADERS = [
    "Ticker",
    "Instrument Type",
    "Action",
    "Target Dollars",
    "Current Dollars",
    "Delta Dollars",
    "Notes",
    "Status",
]

AUDIT_TRAIL_HEADERS = [
    "Detected At",
    "Post URL",
    "Ticker",
    "Signal Type",
    "Stage 1 Rationale",
    "Stage 2 Decision",
    "Stage 2 Reason",
]

REBALANCE_STATUS_VALUES = ["Pending", "Executed", "Skipped"]

# Tab → headers, in creation order. Drives bootstrap.
_TAB_SPEC: list[tuple[str, list[str]]] = [
    (SIGNAL_LOG_TAB, SIGNAL_LOG_HEADERS),
    (BURRY_PORTFOLIO_TAB, BURRY_PORTFOLIO_HEADERS),
    (AGGREGATE_CONSTRAINTS_TAB, AGGREGATE_CONSTRAINTS_HEADERS),
    (REBALANCE_TAB, REBALANCE_HEADERS),
    (AUDIT_TRAIL_TAB, AUDIT_TRAIL_HEADERS),
]


@dataclass
class SignalRecord:
    """A confirmed signal bundled with its Stage 1/2 provenance and source.

    `replay_signals` returns bare `Signal` objects, so the SignalLog and
    AuditTrail provenance columns (Stage 1 rationale, Stage 2 decision and
    reason) travel here alongside the signal. Phase 7 builds these from the
    critic's `filter_candidates` output; the writer stays agnostic about where
    the strings came from.
    """

    signal: Signal
    stage1_rationale: str = ""
    stage2_decision: str = ""
    stage2_reason: str = ""
    source: str = "live"


# --- Small formatting helpers ---------------------------------------------


def _fmt_dt(dt: datetime | None) -> str:
    """ISO-8601 to seconds, or empty string when absent."""
    return dt.isoformat(timespec="seconds") if dt else ""


def _num(value: object) -> object:
    """Render None as empty string; pass numbers through untouched."""
    return "" if value is None else value


def _open(sheets_client: gspread.Client, sheet_id: str) -> gspread.Spreadsheet:
    return sheets_client.open_by_key(sheet_id)


# --- Bootstrap -------------------------------------------------------------


def bootstrap_sheets(sheets_client: gspread.Client, sheet_id: str) -> None:
    """Create the five tabs idempotently and archive the legacy three.

    Re-runnable. A tab that already exists is left exactly as-is: its headers,
    dropdown values, and row contents are preserved. Legacy
    tabs are renamed to `*_archived_2026_05_03` once and never touched again.
    The Status dropdown on Rebalance is applied only when this call creates the
    tab, so a user who edits the dropdown later keeps their edit.
    """
    book = _open(sheets_client, sheet_id)
    existing_titles = {ws.title for ws in book.worksheets()}

    # Archive the legacy schema (rename once, never migrate).
    for legacy in _LEGACY_TABS:
        archived = f"{legacy}{_ARCHIVE_SUFFIX}"
        if legacy in existing_titles and archived not in existing_titles:
            book.worksheet(legacy).update_title(archived)
            existing_titles.discard(legacy)
            existing_titles.add(archived)
            logger.info("Archived legacy tab %r to %r", legacy, archived)

    for title, headers in _TAB_SPEC:
        if title in existing_titles:
            logger.info("Tab %r already exists; leaving untouched", title)
            continue
        ws = book.add_worksheet(
            title=title, rows=1000, cols=max(20, len(headers))
        )
        ws.update(values=[headers], range_name="A1")
        ws.freeze(rows=1)
        if title == REBALANCE_TAB:
            status_col = REBALANCE_HEADERS.index("Status") + 1
            _set_status_dropdown(book, ws, status_col)
        logger.info("Created tab %r with %d header columns", title, len(headers))


def _set_status_dropdown(
    book: gspread.Spreadsheet,
    worksheet: gspread.Worksheet,
    status_col: int,
) -> None:
    """Apply the Pending/Executed/Skipped dropdown to the Status column."""
    request = {
        "setDataValidation": {
            "range": {
                "sheetId": worksheet.id,
                "startRowIndex": 1,
                "endRowIndex": worksheet.row_count,
                "startColumnIndex": status_col - 1,
                "endColumnIndex": status_col,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": v} for v in REBALANCE_STATUS_VALUES
                    ],
                },
                "showCustomUi": True,
                "strict": True,
            },
        }
    }
    book.batch_update({"requests": [request]})


# --- SignalLog (append-only, deduped) -------------------------------------


def _record_dedup_key(record: SignalRecord) -> tuple[str, str, str, str]:
    """The composite SignalLog uniqueness key."""
    sig = record.signal
    return (
        (sig.source_post_url or "").strip(),
        (sig.ticker or "").strip(),
        sig.signal_type.strip(),
        _fmt_dt(sig.valid_time).strip(),
    )


def _existing_signal_keys(
    worksheet: gspread.Worksheet,
) -> set[tuple[str, str, str, str]]:
    """Read the SignalLog and rebuild every row's dedup key.

    Reads back from the same four columns the writer fills (Post URL, Ticker,
    Signal Type, Post Date), so a row written this run reproduces the identical
    key on the next run's read.
    """
    values = worksheet.get_all_values()
    if len(values) <= 1:
        return set()
    headers = values[0]
    idx = {h: i for i, h in enumerate(headers)}
    keys: set[tuple[str, str, str, str]] = set()
    for row in values[1:]:
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        keys.add(
            (
                row[idx["Post URL"]].strip(),
                row[idx["Ticker"]].strip(),
                row[idx["Signal Type"]].strip(),
                row[idx["Post Date"]].strip(),
            )
        )
    return keys


def _signal_log_row(record: SignalRecord) -> list:
    sig = record.signal
    weight_hint = getattr(sig, "weight_hint", None)
    if weight_hint is None:
        weight_hint = getattr(sig, "target_pct", None)
    return [
        _fmt_dt(sig.transaction_time),
        _fmt_dt(sig.valid_time),
        sig.source_post_url,
        sig.signal_type,
        sig.ticker or "",
        getattr(sig, "company_name", "") or "",
        getattr(sig, "exchange", "") or "",
        getattr(sig, "currency", "") or "",
        getattr(sig, "direction", "") or "",
        getattr(sig, "instrument_type", "") or "",
        _num(getattr(sig, "quantity", None)),
        _num(getattr(sig, "strike", None)),
        getattr(sig, "expiration", "") or "",
        _num(weight_hint),
        sig.confidence,
        record.stage1_rationale,
        record.stage2_decision,
        record.stage2_reason,
        sig.evidence_text,
        record.source,
    ]


def append_signals(
    sheets_client: gspread.Client,
    sheet_id: str,
    records: list[SignalRecord],
) -> int:
    """Append unseen signals to SignalLog. Returns the count actually written.

    Logically append-only, but enforces the `(post_url, ticker, signal_type,
    valid_time)` uniqueness check before writing each row, so re-running
    backfill over the same corpus adds zero rows. Duplicates *within* the same
    batch are also collapsed.
    """
    if not records:
        return 0
    book = _open(sheets_client, sheet_id)
    ws = book.worksheet(SIGNAL_LOG_TAB)
    seen = _existing_signal_keys(ws)

    new_rows: list[list] = []
    for record in records:
        key = _record_dedup_key(record)
        if key in seen:
            continue
        seen.add(key)
        new_rows.append(_signal_log_row(record))

    if new_rows:
        # RAW preserves the ISO valid-time string so the dedup key stays stable.
        ws.append_rows(new_rows, value_input_option="RAW")
    logger.info(
        "SignalLog: %d new of %d candidate rows", len(new_rows), len(records)
    )
    return len(new_rows)


# --- BurryPortfolio (full refresh) ----------------------------------------


def _as_naive_utc(dt: datetime) -> datetime:
    """Normalize to naive UTC so aware and naive signal times sort together.

    Date-only posts deserialize timezone-naive while seeded/ingested rows are
    aware; comparing them raises ``TypeError``. Mirrors
    ``portfolio_state._as_naive_utc``.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _signal_enrichment(
    signals: list[Signal] | None,
) -> dict[str, dict[str, object]]:
    """Per-ticker first-seen, last-signal-type, latest-evidence from the log.

    Returns an empty map when `signals` is None, in which case the three
    log-derived BurryPortfolio columns render blank. The materialized
    `PositionState` does not carry these fields, so they come from the raw log.
    """
    out: dict[str, dict[str, object]] = {}
    if not signals:
        return out
    ordered = sorted(signals, key=lambda s: (_as_naive_utc(s.valid_time), _as_naive_utc(s.transaction_time)))
    for sig in ordered:
        if sig.ticker is None:
            continue
        bucket = out.setdefault(
            sig.ticker,
            {"first_seen": sig.valid_time, "last_type": "", "latest_evidence": ""},
        )
        # ordered ascending, so the last write wins for "latest".
        bucket["last_type"] = sig.signal_type
        bucket["latest_evidence"] = sig.evidence_text
    return out


def refresh_burry_portfolio(
    sheets_client: gspread.Client,
    sheet_id: str,
    state: BurryPortfolioState,
    signals: list[Signal] | None = None,
) -> None:
    """Rewrite BurryPortfolio from the materialized state's open positions.

    Only open positions are shown: a closed name is not a current holding, and
    the tab has no status column to disambiguate. Pass the replayed `signals`
    to populate First Seen, Last Signal Type, and Latest Evidence, which the
    `PositionState` model does not track.
    """
    book = _open(sheets_client, sheet_id)
    ws = book.worksheet(BURRY_PORTFOLIO_TAB)
    enrichment = _signal_enrichment(signals)

    out_rows: list[list] = [BURRY_PORTFOLIO_HEADERS]
    for ticker in sorted(state.open_positions()):
        pos = state.positions[ticker]
        extra = enrichment.get(ticker, {})
        out_rows.append(
            [
                pos.ticker,
                pos.instrument_type,
                _num(pos.weight_pct),
                _num(pos.target_pct),
                pos.conviction,
                _fmt_dt(extra.get("first_seen")),  # type: ignore[arg-type]
                _fmt_dt(pos.last_confirmed_at),
                extra.get("last_type", ""),
                extra.get("latest_evidence", ""),
            ]
        )

    ws.clear()
    ws.update(values=out_rows, range_name="A1")
    logger.info("BurryPortfolio: wrote %d open positions", len(out_rows) - 1)


# --- AggregateConstraints (full refresh) ----------------------------------


def _latest_cap_signals(
    signals: list[Signal] | None,
) -> dict[str, AggregateCapSignal]:
    """Latest AggregateCapSignal per grouping, by valid_time."""
    latest: dict[str, AggregateCapSignal] = {}
    if not signals:
        return latest
    for sig in signals:
        if not isinstance(sig, AggregateCapSignal):
            continue
        prev = latest.get(sig.grouping)
        if prev is None or sig.valid_time >= prev.valid_time:
            latest[sig.grouping] = sig
    return latest


def refresh_constraints(
    sheets_client: gspread.Client,
    sheet_id: str,
    state: BurryPortfolioState,
    signals: list[Signal] | None = None,
) -> None:
    """Rewrite AggregateConstraints from the state's caps.

    Pass the replayed `signals` to enrich each cap with the source signal's
    date and evidence quote; without them those two columns render blank.
    """
    book = _open(sheets_client, sheet_id)
    ws = book.worksheet(AGGREGATE_CONSTRAINTS_TAB)
    cap_signals = _latest_cap_signals(signals)

    out_rows: list[list] = [AGGREGATE_CONSTRAINTS_HEADERS]
    for grouping in sorted(state.caps):
        cap_pct = state.caps[grouping]
        src = cap_signals.get(grouping)
        out_rows.append(
            [
                grouping,
                _num(cap_pct),
                _fmt_dt(src.valid_time) if src else "",
                src.evidence_text if src else "",
            ]
        )

    ws.clear()
    ws.update(values=out_rows, range_name="A1")
    logger.info("AggregateConstraints: wrote %d caps", len(out_rows) - 1)


# --- Rebalance (full refresh) ---------------------------------------------


def write_rebalance(
    sheets_client: gspread.Client,
    sheet_id: str,
    actions: list[RebalanceAction],
) -> None:
    """Rewrite the Rebalance tab with the current actions, Status = Pending.

    The tab is refreshed in full each cycle: recommendations are recomputed
    from scratch, so a prior cycle's Status edits do not carry meaning once the
    actions change. `worksheet.clear()` clears values only, so the Status
    dropdown rule installed at bootstrap survives and re-applies to the new
    rows. `RebalanceAction` does not carry an instrument type, so that column is
    left blank; the Notes column flags options positions instead.
    """
    book = _open(sheets_client, sheet_id)
    ws = book.worksheet(REBALANCE_TAB)

    out_rows: list[list] = [REBALANCE_HEADERS]
    for action in actions:
        target_dollars = round(action.current_value_usd + action.delta_usd, 2)
        out_rows.append(
            [
                action.ticker,
                "",  # instrument type is not on RebalanceAction (see docstring)
                action.direction,
                target_dollars,
                action.current_value_usd,
                action.delta_usd,
                action.notes,
                "Pending",
            ]
        )

    ws.clear()
    ws.update(values=out_rows, range_name="A1", value_input_option="USER_ENTERED")
    logger.info("Rebalance: wrote %d actions", len(out_rows) - 1)


# --- AuditTrail (append) --------------------------------------------------


def _audit_row(record: SignalRecord) -> list:
    sig = record.signal
    return [
        _fmt_dt(sig.transaction_time),
        sig.source_post_url,
        sig.ticker or "",
        sig.signal_type,
        record.stage1_rationale,
        record.stage2_decision,
        record.stage2_reason,
    ]


def append_audit_trail(
    sheets_client: gspread.Client,
    sheet_id: str,
    records: list[SignalRecord],
) -> int:
    """Append per-signal critic decision rows to AuditTrail. Returns the count.

    The audit trail is an explainability log, so every record is appended
    verbatim with no dedup gate: a re-vetted signal is a legitimately new audit
    entry even when its SignalLog row already exists.
    """
    if not records:
        return 0
    book = _open(sheets_client, sheet_id)
    ws = book.worksheet(AUDIT_TRAIL_TAB)
    rows = [_audit_row(r) for r in records]
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info("AuditTrail: appended %d rows", len(rows))
    return len(rows)
