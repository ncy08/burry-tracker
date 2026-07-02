"""Three-channel notifier: Gmail draft + macOS banner + Twilio SMS.

Concurrent dispatcher (`notify_all`) fires all three channels in parallel
threads and tolerates partial failure. A run with zero rebalance actions is
silent.

The payload is the rebalance summary (`NotificationPayload`): a sheet URL, a
rebalance count, the top actions by absolute dollar delta, and the total dollar
movement. Each channel has its own renderer (`_gmail_html`, `_macos_text`,
`_twilio_text`) so the rendered output is unit-testable without sending.

Gmail draft dedup: the drafts API does not deduplicate, so we search-and-delete
prior drafts (subject prefix `[substack-trader]`) before creating the new one.
That collapses repeated scheduled fires to a single always-current draft.
"""

from __future__ import annotations

import base64
import concurrent.futures
import logging
import subprocess
from datetime import date
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Protocol

from substack_trader.config import Config
from substack_trader.models import NotificationPayload

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

logger = logging.getLogger(__name__)

DRAFT_SUBJECT_PREFIX = "[substack-trader]"


class Notifier(Protocol):
    def notify(self, payload: NotificationPayload) -> None: ...


# --- Shared formatting -----------------------------------------------------


def _fmt_total(payload: NotificationPayload) -> str:
    """Total dollar movement, e.g. '$1,500'."""
    return f"${payload.total_delta_usd:,.0f}"


def _ticker_list(payload: NotificationPayload) -> str:
    """Comma-joined top-action tickers, e.g. 'GME, AAPL, TSLA'."""
    return ", ".join(a.ticker for a in payload.top_actions)


def _action_phrase(count: int) -> str:
    return "rebalance action" if count == 1 else "rebalance actions"


def _macos_text(payload: NotificationPayload) -> str:
    """Short banner string. Includes count, total, tickers, and sheet URL."""
    n = payload.rebalance_count
    parts = [f"{n} Burry {_action_phrase(n)} ({_fmt_total(payload)} to move)"]
    if payload.top_actions:
        parts.append(_ticker_list(payload))
    parts.append(payload.sheet_url)
    return " — ".join(parts)


def _twilio_text(payload: NotificationPayload) -> str:
    """SMS string. Includes count, total, tickers, and sheet URL."""
    n = payload.rebalance_count
    head = f"{n} Burry {_action_phrase(n)} ({_fmt_total(payload)} to move)"
    tickers = f": {_ticker_list(payload)}" if payload.top_actions else ""
    return f"{head}{tickers}\n{payload.sheet_url}"


def _gmail_html(payload: NotificationPayload) -> str:
    """Gmail draft HTML body: one row per top action plus a total line."""
    rows: list[str] = []
    for action in payload.top_actions:
        sign = "+" if action.delta_dollars >= 0 else "-"
        rows.append(
            f"""
<tr>
  <td style='padding:6px 12px;font-weight:600'>{action.ticker}</td>
  <td style='padding:6px 12px;text-align:right'>{sign}${abs(action.delta_dollars):,.2f}</td>
</tr>
""".strip()
        )
    table = (
        "<table style='border-collapse:collapse;margin:8px 0'>"
        "<tr><th style='text-align:left;padding:6px 12px'>Ticker</th>"
        "<th style='text-align:right;padding:6px 12px'>Delta</th></tr>"
        f"{''.join(rows)}</table>"
        if rows
        else "<p>No top actions.</p>"
    )
    n = payload.rebalance_count
    return (
        "<html><body style='font-family:-apple-system,Helvetica,Arial,sans-serif;line-height:1.4'>"
        f"<p style='font-size:15px;font-weight:600'>{n} {_action_phrase(n)} pending "
        f"({_fmt_total(payload)} total to move)</p>"
        f"{table}"
        f"<p style='margin-top:12px'>Full detail: "
        f"<a href='{payload.sheet_url}'>{payload.sheet_url}</a></p>"
        "</body></html>"
    )


# --- Channels --------------------------------------------------------------


class GmailDraftNotifier:
    def __init__(self, gmail_service: Resource) -> None:
        self.gmail = gmail_service

    def notify(self, payload: NotificationPayload) -> None:
        self._delete_prior_drafts()
        subject = (
            f"{DRAFT_SUBJECT_PREFIX} Burry rebalance: {payload.rebalance_count} "
            f"pending — {date.today():%a %b %d}"
        )
        html = _gmail_html(payload)
        msg = MIMEText(html, "html")
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        self.gmail.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        logger.info("Created Gmail draft: %s", subject)

    def _delete_prior_drafts(self) -> None:
        """Remove any prior drafts with our subject-line marker."""
        try:
            resp = (
                self.gmail.users()
                .drafts()
                .list(userId="me", q=f'subject:"{DRAFT_SUBJECT_PREFIX} Burry rebalance"')
                .execute()
            )
            for draft in resp.get("drafts", []):
                self.gmail.users().drafts().delete(userId="me", id=draft["id"]).execute()
            if resp.get("drafts"):
                logger.info("Deleted %d prior drafts", len(resp.get("drafts", [])))
        except Exception as exc:
            logger.warning("Failed to clear prior drafts: %s", exc)


class MacOSNotifier:
    def notify(self, payload: NotificationPayload) -> None:
        n = payload.rebalance_count
        title = "Substack Trader"
        body = f"{n} Burry {_action_phrase(n)} pending"
        body_safe = body.replace('"', '\\"')
        title_safe = title.replace('"', '\\"')
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{body_safe}" with title "{title_safe}" sound name "Ping"',
            ],
            check=False,
        )
        logger.info("macOS banner fired: %s", body)


class TwilioNotifier:
    def __init__(self, config: Config) -> None:
        self.config = config

    def notify(self, payload: NotificationPayload) -> None:
        cfg = self.config
        if not (cfg.twilio_account_sid and cfg.twilio_auth_token and cfg.twilio_from_number and cfg.twilio_to_number):
            logger.warning("Twilio unconfigured; skipping SMS")
            return
        from twilio.rest import Client as TwilioClient

        client = TwilioClient(cfg.twilio_account_sid, cfg.twilio_auth_token)
        client.messages.create(
            body=_twilio_text(payload),
            from_=cfg.twilio_from_number,
            to=cfg.twilio_to_number,
        )
        logger.info("Twilio SMS sent")


def notify_all(payload: NotificationPayload, config: Config) -> None:
    """Fire all three channels concurrently. No-op if rebalance_count == 0.

    Per-channel exceptions are caught and logged. Partial delivery is
    acceptable; we never raise back to the caller.
    """
    if payload.rebalance_count == 0:
        logger.info("No rebalance actions; notifications skipped")
        return

    # Gmail draft requires a service. Lazily import here to avoid a hard
    # dependency at import time for callers that only need the macOS banner
    # (e.g. test-notify in environments without OAuth).
    notifiers: list[tuple[str, Notifier]] = [("macos", MacOSNotifier()), ("twilio", TwilioNotifier(config))]

    try:
        from substack_trader.auth import load_clients_headless

        gmail_service, _ = load_clients_headless(config)
        notifiers.insert(0, ("gmail-draft", GmailDraftNotifier(gmail_service)))
    except Exception as exc:
        logger.warning("Skipping Gmail draft notifier: %s", exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(n.notify, payload): name for name, n in notifiers}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                logger.error("Notifier %s failed: %s", name, exc)
