"""Pipeline orchestrator: one scheduled run from Gmail to notifications.

`run_one_cycle` is the heart of every scheduled fire. It wraps the entire body
in an exclusive file lock (concurrency guard) so a slow morning fire cannot
collide with a later one and double-write.

Cycle stages, inside the lock:
  1. Stage 1 extraction on each new Burry post.
  2. Stage 2 critic on each candidate.
  3. Persist confirmed signals to SQLite with their critic decision records.
  4. Replay the event log and refresh the BurryPortfolio state tab.
  5. Run the constraint solver against the latest aggregate caps.
  6. Load the user portfolio CSV and compute rebalance actions.
  7. Write all five Sheet tabs.
  8. Send the rebalance notification when any action survives the threshold.
  9. Regenerate the read-only dashboard mirror (guarded; never crashes a cycle).

Idempotency: a post whose URL already produced signals in the
SQLite event log is skipped before extraction, so re-running a cycle against the
same inputs makes no new writes. The Gmail forward-only cursor is a small file in the state dir.

`process_post` and `refresh_derived_tabs` are shared with `backfill.py` so a
live run and a backfill produce the same final state.
"""

from __future__ import annotations

import fcntl
import logging
import subprocess
from pathlib import Path

from substack_trader.auth import load_clients_headless
from substack_trader.config import Config
from substack_trader.critic import filter_candidates
from substack_trader.db import init_db, insert_signal, replay_signals
from substack_trader.extractor import extract_candidates
from substack_trader.gmail_reader import fetch_new_posts
from substack_trader.models import (
    BurryPost,
    NotificationPayload,
    RebalanceActionSummary,
)
from substack_trader.notifier import notify_all
from substack_trader.portfolio_state import materialize_state
from substack_trader.rebalance import RebalanceAction, compute_rebalance
from substack_trader.sheet_writer import (
    SignalRecord,
    append_audit_trail,
    append_signals,
    refresh_burry_portfolio,
    refresh_constraints,
    write_rebalance,
)
from substack_trader.user_portfolio import load_user_portfolio

logger = logging.getLogger(__name__)


def _sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


def _cursor_path(config: Config) -> Path:
    return config.state_dir / "gmail_cursor.txt"


def _read_cursor(config: Config) -> str | None:
    path = _cursor_path(config)
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _write_cursor(config: Config, message_id: str) -> None:
    _cursor_path(config).write_text(message_id, encoding="utf-8")


# --- Shared per-post processing -------------------------------------------


def process_post(
    post: BurryPost,
    config: Config,
    *,
    source: str = "live",
) -> list[SignalRecord]:
    """Stage 1 + Stage 2 for one post; persist confirmed signals to SQLite.

    Returns a `SignalRecord` for every extracted signal (confirmed and vetoed),
    each carrying its Stage 1 rationale and Stage 2 decision. Only confirmed
    signals are written to the SQLite event log: a veto is a false positive and
    must not move portfolio state. The full record list feeds the SignalLog and
    AuditTrail Sheet tabs, where the Stage 2 decision column distinguishes the
    two outcomes.
    """
    candidates = extract_candidates(
        post.body_text,
        config,
        source_post_url=post.post_url or post.gmail_message_id,
        valid_time=post.pub_date,
    )
    records: list[SignalRecord] = []
    for signal, candidate, verdict in filter_candidates(candidates, config):
        rationale = candidate.get("rationale", "")
        records.append(
            SignalRecord(
                signal=signal,
                stage1_rationale=rationale,
                stage2_decision=verdict.verdict,
                stage2_reason=verdict.reason,
                source=source,
            )
        )
        if verdict.verdict == "confirm":
            insert_signal(
                signal,
                stage1_rationale=rationale,
                stage2_decision=verdict.verdict,
                stage2_reason=verdict.reason,
                config=config,
            )
    return records


def refresh_derived_tabs(
    sheets_client,
    config: Config,
) -> list[RebalanceAction]:
    """Replay the event log, refresh state/constraints, write the Rebalance tab.

    Returns the rebalance actions (already filtered to those above
    `min_rebalance_usd` by `compute_rebalance`). When the user portfolio CSV is
    absent, the state and constraint tabs still refresh, no Rebalance rows are
    written, and an empty list is returned.
    """
    signals = replay_signals(config=config)
    state = materialize_state(signals)
    refresh_burry_portfolio(sheets_client, config.sheet_id, state, signals=signals)
    refresh_constraints(sheets_client, config.sheet_id, state, signals=signals)

    try:
        user_portfolio = load_user_portfolio(config.user_portfolio_csv_path)
    except FileNotFoundError:
        logger.warning(
            "User portfolio CSV missing at %s; skipping rebalance computation",
            config.user_portfolio_csv_path,
        )
        return []

    actions = compute_rebalance(
        state,
        user_portfolio,
        config.risk_multiplier,
        min_rebalance_usd=config.min_rebalance_usd,
        burry_signals=signals,
    )
    write_rebalance(sheets_client, config.sheet_id, actions)
    return actions


def _build_payload(
    config: Config, actions: list[RebalanceAction]
) -> NotificationPayload:
    """Build the rebalance-summary notification payload (top 3 by abs delta)."""
    ranked = sorted(actions, key=lambda a: abs(a.delta_usd), reverse=True)
    top = [
        RebalanceActionSummary(ticker=a.ticker, delta_dollars=a.delta_usd)
        for a in ranked[:3]
    ]
    total = round(sum(abs(a.delta_usd) for a in actions), 2)
    return NotificationPayload(
        sheet_url=_sheet_url(config.sheet_id),
        rebalance_count=len(actions),
        top_actions=top,
        total_delta_usd=total,
    )


# --- Dashboard render-failure alert (infra, not trade) ---------------------
# `notifier.py` is a locked module and its `.notify()` only renders rebalance
# summaries, so these mirror its osascript / Twilio calls to carry a raw error
# string. The Gmail draft is intentionally excluded — it is reserved for trade
# signals, so an infra error never pollutes it.


def _alert_macos(message: str) -> None:
    """Fire a macOS banner carrying a raw text message."""
    title = "Substack Trader"
    body_safe = message.replace('"', '\\"')
    title_safe = title.replace('"', '\\"')
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{body_safe}" with title "{title_safe}" sound name "Basso"',
        ],
        check=False,
    )


def _alert_twilio(message: str, config: Config) -> None:
    """Send a render-failure SMS via Twilio. No-op when Twilio is unconfigured."""
    cfg = config
    if not (
        cfg.twilio_account_sid
        and cfg.twilio_auth_token
        and cfg.twilio_from_number
        and cfg.twilio_to_number
    ):
        logger.warning("Twilio unconfigured; skipping render-failure SMS")
        return
    from twilio.rest import Client as TwilioClient

    client = TwilioClient(cfg.twilio_account_sid, cfg.twilio_auth_token)
    client.messages.create(body=message, from_=cfg.twilio_from_number, to=cfg.twilio_to_number)
    logger.info("Twilio render-failure SMS sent")


def _alert_render_failure(exc: Exception, config: Config) -> None:
    """Best-effort macOS + Twilio alert that the dashboard render failed.

    Each channel is isolated so an alerting failure can never re-raise into the
    cycle. The Gmail draft is deliberately not used.
    """
    message = f"Burry dashboard render failed: {exc}"
    for label, send in (
        ("macos", lambda: _alert_macos(message)),
        ("twilio", lambda: _alert_twilio(message, config)),
    ):
        try:
            send()
        except Exception as alert_exc:  # an alert failure must not crash the cycle
            logger.error("Render-failure alert via %s failed: %s", label, alert_exc)


# --- Cycle entry points ----------------------------------------------------


def run_one_cycle(config: Config) -> dict:
    """Execute one scheduled run. Returns a structured result for logging.

    Output keys:
      - posts_processed: int
      - signals_confirmed: int
      - rebalance_count: int
      - skipped: str (when the lock could not be acquired)
      - error: str (on unrecoverable failure inside the lock)
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / "run.lock"
    try:
        lock_fd = open(lock_path, "w")
    except OSError as exc:
        logger.error("Cannot open lock file %s: %s", lock_path, exc)
        return {"error": f"lock-open: {exc}"}

    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Previous run still in progress; exiting cleanly.")
            return {"skipped": "lock_held"}

        try:
            return _cycle_body(config)
        except Exception as exc:
            logger.exception("run_one_cycle failed")
            return {"error": str(exc)}
    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fd.close()


def _cycle_body(config: Config) -> dict:
    gmail_service, sheets_client = load_clients_headless(config)
    init_db(config)

    # Dedup gate: any post URL already in the event log is done.
    processed_urls = {s.source_post_url for s in replay_signals(config=config)}

    cursor = _read_cursor(config)
    posts = fetch_new_posts(
        gmail_service,
        since_message_id=cursor,
        label=config.gmail_label,
        sender=config.gmail_sender_filter,
    )

    all_records: list[SignalRecord] = []
    posts_processed = 0
    for post in posts:
        post_key = post.post_url or post.gmail_message_id
        if post_key in processed_urls:
            logger.info("Skipping already-processed %s", post_key)
            continue
        all_records.extend(process_post(post, config, source="live"))
        posts_processed += 1

    confirmed = [r for r in all_records if r.stage2_decision == "confirm"]
    if all_records:
        append_signals(sheets_client, config.sheet_id, all_records)
        append_audit_trail(sheets_client, config.sheet_id, all_records)

    # Stages 4-7: refresh the state-derived tabs from the full event log.
    actions = refresh_derived_tabs(sheets_client, config)

    # Forward-only cursor advances to the newest fetched post (reverse-chrono).
    if posts:
        _write_cursor(config, posts[0].gmail_message_id)

    # Stage 8: notify when any action survived the min-rebalance threshold.
    if actions:
        notify_all(_build_payload(config, actions), config)

    # Stage 9: regenerate the read-only dashboard mirror. Sits OUTSIDE the
    # `if actions:` block (it refreshes every cycle, even quiet ones) and is
    # fully guarded — a render failure must NEVER crash the money-moving cycle.
    try:
        from substack_trader import render_dashboard

        dashboard_path = render_dashboard.render(config)
        logger.info("Dashboard rendered: %s", dashboard_path)
    except Exception as exc:
        logger.exception("Dashboard render failed")
        _alert_render_failure(exc, config)

    logger.info(
        "Cycle complete: posts_processed=%d signals_confirmed=%d rebalance_count=%d",
        posts_processed,
        len(confirmed),
        len(actions),
    )
    return {
        "posts_processed": posts_processed,
        "signals_confirmed": len(confirmed),
        "rebalance_count": len(actions),
    }
