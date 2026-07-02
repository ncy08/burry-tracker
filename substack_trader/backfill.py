"""One-time historical seed for the SQLite event log and five-tab Sheet.

Two tracks, run sequentially:

Track A — Q3 2025 13F seed: hardcoded Scion Asset Management holdings as of the
last public 13F filing (pre-deregistration). Each holding enters the event log
as a `PositionDisclosureSignal` (an open holding disclosed by the filing, not a
fresh execution) dated to the filing.

Track B — Substack web archive scrape: navigate the public Substack archive via
the browse binary (or read a local corpus), filter to posts published on or
after 2025-11-23, run the two-stage extractor on each body, and persist the
confirmed signals to SQLite.

After both tracks, the event log is replayed once to materialize state and write
the BurryPortfolio, AggregateConstraints, and Rebalance tabs. A backfill and a
live run over the same posts therefore converge on the same final state.

`--use-local` reads bodies from `data/raw/` instead of live-scraping,
which makes backfill replayable without Substack cookies.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path

from substack_trader.auth import load_clients_headless
from substack_trader.config import Config
from substack_trader.db import init_db, insert_signal, replay_signals
from substack_trader.models import BurryPost
from substack_trader.pipeline import process_post, refresh_derived_tabs
from substack_trader.sheet_writer import (
    SignalRecord,
    append_audit_trail,
    append_signals,
)
from substack_trader.signals import PositionDisclosureSignal

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://michaeljburry.substack.com/archive"
POST_URL_RE = re.compile(r"^https://michaeljburry\.substack\.com/p/[a-z0-9-]+$")
EARLIEST_POST_DATE = date(2025, 11, 23)  # Cassandra Unchained launch
LOCAL_CORPUS_DIR = Path(__file__).parent.parent / "data" / "raw"
THROTTLE_SECS = 6.0  # polite delay between live fetches (rate-limit margin)

# Track A provenance. All 8 disclosures share this source URL and date, so a
# second backfill finds the URL already in the event log and skips the seed.
SEC_13F_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    "&CIK=0001649339&type=13F"
)
SEC_13F_DATE = datetime(2025, 11, 14, tzinfo=timezone.utc)

# (ticker, instrument_type, evidence_text)
SCION_Q3_2025: list[tuple[str, str, str]] = [
    ("NVDA", "put", "Scion Q3 2025 13F: put options on 1,000,000 NVDA shares (~$186.6M notional)"),
    ("PLTR", "put", "Scion Q3 2025 13F: put options on PLTR (major short)"),
    ("MOH", "stock", "Scion Q3 2025 13F: long MOH (~35% of equity portion)"),
    ("LULU", "stock", "Scion Q3 2025 13F: long LULU (~26% of equity portion)"),
    ("SLM", "stock", "Scion Q3 2025 13F: long SLM (~19.5% of equity portion)"),
    ("BRKR", "stock", "Scion Q3 2025 13F: long BRKR (~19.3% of equity portion)"),
    ("PFE", "call", "Scion Q3 2025 13F: PFE call options (catalyst position)"),
    ("HAL", "call", "Scion Q3 2025 13F: HAL call options (catalyst position)"),
]


def _browse_path() -> Path:
    """Locate the headless-browser CLI; raise with a hint if missing."""
    env_override = os.environ.get("BROWSE_BIN", "")
    candidates = [
        *([Path(env_override)] if env_override else []),
        Path.cwd() / "browse" / "dist" / "browse",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "Headless-browser CLI not found. Set BROWSE_BIN to a CLI that "
        "supports goto/js/links/text/cookies subcommands (see README)."
    )


def _verify_substack_cookies(browse_bin: Path) -> None:
    """Raise if the browse session has zero substack.com cookies."""
    proc = subprocess.run(
        [str(browse_bin), "cookies"], capture_output=True, text=True, check=False
    )
    if "substack.com" not in proc.stdout:
        raise RuntimeError(
            "Substack cookies missing: authenticate the browser session "
            "with your paid-subscriber cookies first"
        )


def _date_from_slug(slug: str) -> date | None:
    """Parse '...april-23-2026' style suffixes into a date when possible."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.search(r"-(?:january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})-(\d{4})$", slug)
    if not m:
        return None
    month_name = re.search(r"-(january|february|march|april|may|june|july|august|september|october|november|december)-", slug)
    if not month_name:
        return None
    month = months[month_name.group(1)]
    try:
        return date(int(m.group(2)), month, int(m.group(1)))
    except ValueError:
        return None


def _seed_13f(
    config: Config,
    *,
    dry_run: bool,
    processed_urls: set[str],
) -> list[SignalRecord]:
    """Track A: the 8 Scion Q3 2025 holdings as PositionDisclosure signals."""
    if SEC_13F_URL in processed_urls:
        print("Track A: 13F seed already present in event log; skipping")
        return []

    signals = [
        PositionDisclosureSignal(
            ticker=ticker,
            instrument_type=instrument,  # type: ignore[arg-type]
            evidence_text=evidence,
            confidence="high",
            source_post_url=SEC_13F_URL,
            valid_time=SEC_13F_DATE,
            transaction_time=datetime.now(tz=timezone.utc),
        )
        for ticker, instrument, evidence in SCION_Q3_2025
    ]

    print(f"Track A: 8 13F seed entries — {'DRY RUN' if dry_run else 'writing'}")
    if dry_run:
        for sig in signals:
            print(f"  {sig.ticker:6s} {sig.instrument_type:5s} {sig.evidence_text[:70]}")
        return []

    records: list[SignalRecord] = []
    for sig in signals:
        insert_signal(
            sig,
            stage1_rationale="13F historical seed",
            stage2_decision="confirm",
            stage2_reason="historical anchor (not critic-vetted)",
            config=config,
        )
        records.append(
            SignalRecord(
                signal=sig,
                stage1_rationale="13F historical seed",
                stage2_decision="confirm",
                stage2_reason="historical anchor (not critic-vetted)",
                source="13f-q3-2025",
            )
        )
    return records


def _local_post_paths() -> list[Path]:
    if not LOCAL_CORPUS_DIR.exists():
        return []
    return sorted(p for p in LOCAL_CORPUS_DIR.glob("*.txt") if p.name != "manifest.jsonl")


def _post_from_local(path: Path) -> BurryPost:
    body = path.read_text(encoding="utf-8")
    slug = path.stem
    url = f"https://michaeljburry.substack.com/p/{slug}"
    pub = _date_from_slug(slug) or date(2026, 1, 1)
    return BurryPost(
        gmail_message_id="",
        post_url=url,
        title=slug.replace("-", " "),
        pub_date=datetime.combine(pub, datetime.min.time(), tzinfo=timezone.utc),
        body_text=body,
        legs=[],
    )


def _post_urls_from_archive(browse_bin: Path) -> list[str]:
    """Navigate the archive page and extract unique post URLs (post-scroll)."""
    subprocess.run([str(browse_bin), "goto", ARCHIVE_URL], capture_output=True, check=True)
    # Trigger lazy-load of older posts
    for _ in range(6):
        subprocess.run(
            [str(browse_bin), "js", "window.scrollTo(0, document.body.scrollHeight)"],
            capture_output=True,
        )
    proc = subprocess.run(
        [str(browse_bin), "links"], capture_output=True, text=True, check=True
    )
    urls: list[str] = []
    for line in proc.stdout.splitlines():
        for token in re.findall(r"https://michaeljburry\.substack\.com/p/[a-z0-9-]+", line):
            if POST_URL_RE.match(token) and token not in urls:
                urls.append(token)
    return urls


def _post_from_live(browse_bin: Path, url: str) -> BurryPost | None:
    subprocess.run([str(browse_bin), "goto", url], capture_output=True, check=False)
    time.sleep(0.8)
    proc = subprocess.run(
        [str(browse_bin), "text"], capture_output=True, text=True, check=False
    )
    body = proc.stdout
    body = re.sub(r"^--- BEGIN UNTRUSTED EXTERNAL CONTENT[^\n]*\n", "", body)
    body = re.sub(r"\n--- END UNTRUSTED EXTERNAL CONTENT ---\s*$", "", body).strip()
    if "Too Many Requests" in body and len(body) < 100:
        logger.warning("Rate-limited on %s; sleeping 30s", url)
        time.sleep(30)
        return None
    if len(body) < 200:
        logger.warning("Body too short on %s (%d bytes); skipping", url, len(body))
        return None
    slug = url.rsplit("/", 1)[-1]
    pub = _date_from_slug(slug) or date(2026, 1, 1)
    return BurryPost(
        gmail_message_id="",
        post_url=url,
        title=slug.replace("-", " "),
        pub_date=datetime.combine(pub, datetime.min.time(), tzinfo=timezone.utc),
        body_text=body,
        legs=[],
    )


def _scrape_substack(
    config: Config,
    *,
    dry_run: bool,
    use_local: bool,
    processed_urls: set[str],
) -> list[SignalRecord]:
    """Track B: extract + persist every Burry post since 2025-11-23.

    `use_local=True`: read bodies from data/raw/.
    `use_local=False`: live-scrape via the browse binary (requires cookies).
    """
    if use_local:
        paths = _local_post_paths()
        print(f"Track B: {len(paths)} posts loaded from local corpus")
        posts = [_post_from_local(p) for p in paths]
    else:
        browse_bin = _browse_path()
        _verify_substack_cookies(browse_bin)
        urls = _post_urls_from_archive(browse_bin)
        print(f"Track B: {len(urls)} post URLs discovered from archive")
        posts = []
        for i, url in enumerate(urls, 1):
            slug_date = _date_from_slug(url.rsplit("/", 1)[-1])
            if slug_date and slug_date < EARLIEST_POST_DATE:
                continue
            print(f"  [{i:02d}/{len(urls)}] fetching {url}")
            post = _post_from_live(browse_bin, url)
            if post is not None:
                posts.append(post)
            time.sleep(THROTTLE_SECS)

    posts = [p for p in posts if p.pub_date.date() >= EARLIEST_POST_DATE]
    print(f"Track B: {len(posts)} posts pass date filter (>= {EARLIEST_POST_DATE.isoformat()})")

    if dry_run:
        for post in posts[:10]:
            print(f"  {post.pub_date.date()} {post.post_url}")
        if len(posts) > 10:
            print(f"  ... ({len(posts) - 10} more)")
        return []

    records: list[SignalRecord] = []
    for i, post in enumerate(posts, 1):
        if post.post_url in processed_urls:
            print(f"  [{i:02d}/{len(posts)}] SKIP {post.post_url} (already in event log)")
            continue
        try:
            post_records = process_post(post, config, source="backfill-substack")
        except Exception as exc:
            logger.error("Extraction failed for %s: %s", post.post_url, exc)
            continue
        confirmed = sum(1 for r in post_records if r.stage2_decision == "confirm")
        print(f"  [{i:02d}/{len(posts)}] {post.post_url} ({confirmed} confirmed signals)")
        records.extend(post_records)
    return records


def run_backfill(config: Config, *, dry_run: bool, use_local: bool) -> int:
    """Entry point used by `python -m substack_trader backfill`."""
    if dry_run:
        print("DRY RUN — no SQLite or Sheet writes")

    sheets_client = None
    processed_urls: set[str] = set()
    if not dry_run:
        if not config.sheet_id:
            print("SUBSTACK_TRADER_SHEET_ID is unset; populate .env first.")
            return 2
        _, sheets_client = load_clients_headless(config)
        init_db(config)
        processed_urls = {s.source_post_url for s in replay_signals(config=config)}

    records: list[SignalRecord] = []
    records += _seed_13f(config, dry_run=dry_run, processed_urls=processed_urls)
    records += _scrape_substack(
        config, dry_run=dry_run, use_local=use_local, processed_urls=processed_urls
    )

    if not dry_run:
        if records:
            append_signals(sheets_client, config.sheet_id, records)
            append_audit_trail(sheets_client, config.sheet_id, records)
        refresh_derived_tabs(sheets_client, config)

    confirmed = sum(1 for r in records if r.stage2_decision == "confirm")
    print(f"\nbackfill complete: {len(records)} signals extracted, {confirmed} confirmed")
    return 0
