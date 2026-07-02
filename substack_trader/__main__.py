"""CLI entry point for the substack_trader pipeline.

Usage:
    python -m substack_trader auth                One-time OAuth bootstrap (browser)
    python -m substack_trader bootstrap           Create the five Sheet tabs
    python -m substack_trader run                 One scheduled cycle
    python -m substack_trader extract --message-id ID  Debug helper
    python -m substack_trader extract --file PATH      Debug helper (offline)
    python -m substack_trader backfill [--dry-run] [--use-local]
                                                  Historical seed
    python -m substack_trader test-notify         Fire a test payload on all 3 channels
    python -m substack_trader install-service     Install launchd plist
    python -m substack_trader uninstall-service   Uninstall
    python -m substack_trader status              Show service status

CRITICAL: every subcommand except `auth` calls `load_clients_headless`.
Any code path reachable by launchd that calls `bootstrap_oauth_interactive`
is a regression of M1 from Review 1: the daemon has no GUI session and
will hang silently waiting for browser consent that never arrives.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from substack_trader.config import Config

logger = logging.getLogger(__name__)


def cmd_auth() -> int:
    from substack_trader.auth import bootstrap_oauth_interactive

    cfg = Config.load()
    bootstrap_oauth_interactive(cfg)
    return 0


def cmd_bootstrap() -> int:
    from substack_trader.auth import load_clients_headless
    from substack_trader.sheet_writer import bootstrap_sheets

    cfg = Config.load()
    if not cfg.sheet_id:
        print("SUBSTACK_TRADER_SHEET_ID is unset; populate .env first.", file=sys.stderr)
        return 2
    _, sc = load_clients_headless(cfg)
    bootstrap_sheets(sc, cfg.sheet_id)
    print(f"Bootstrap complete: https://docs.google.com/spreadsheets/d/{cfg.sheet_id}")
    return 0


def cmd_run() -> int:
    from substack_trader.pipeline import run_one_cycle

    cfg = Config.load()
    result = run_one_cycle(cfg)
    print(json.dumps(result, default=str))
    return 0 if "error" not in result else 1


def cmd_extract(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from substack_trader.extractor import extract_candidates

    cfg = Config.load()
    if args.file:
        body = open(args.file, encoding="utf-8").read()
        source_url = f"file://{args.file}"
    elif args.message_id:
        from substack_trader.auth import load_clients_headless
        from substack_trader.gmail_reader import _extract_body_text

        gmail_service, _ = load_clients_headless(cfg)
        msg = (
            gmail_service.users()
            .messages()
            .get(userId="me", id=args.message_id, format="full")
            .execute()
        )
        body = _extract_body_text(msg.get("payload", {}))
        source_url = f"gmail://{args.message_id}"
    else:
        print("--message-id or --file required", file=sys.stderr)
        return 2
    candidates = extract_candidates(
        body, cfg, source_post_url=source_url, valid_time=datetime.now(tz=timezone.utc)
    )
    print(
        json.dumps(
            [signal.model_dump(mode="json") for signal, _ in candidates],
            default=str,
            indent=2,
        )
    )
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    from substack_trader.backfill import run_backfill

    cfg = Config.load()
    return run_backfill(cfg, dry_run=args.dry_run, use_local=args.use_local)


def cmd_test_notify() -> int:
    from substack_trader.models import NotificationPayload, RebalanceActionSummary
    from substack_trader.notifier import notify_all

    cfg = Config.load()
    payload = NotificationPayload(
        sheet_url=f"https://docs.google.com/spreadsheets/d/{cfg.sheet_id}"
        if cfg.sheet_id
        else "https://example.test",
        rebalance_count=2,
        top_actions=[
            RebalanceActionSummary(ticker="GME", delta_dollars=1200.0),
            RebalanceActionSummary(ticker="NVDA", delta_dollars=-450.0),
        ],
        total_delta_usd=1650.0,
    )
    notify_all(payload, cfg)
    return 0


def cmd_install_service() -> int:
    from substack_trader.service import cmd_install_service as install

    return install()


def cmd_uninstall_service() -> int:
    from substack_trader.service import cmd_uninstall_service as uninstall

    return uninstall()


def cmd_status() -> int:
    from substack_trader.service import cmd_service_status

    return cmd_service_status()


def main() -> int:
    parser = argparse.ArgumentParser(prog="substack_trader", description="Burry trade signal pipeline")
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser("auth", help="OAuth bootstrap (browser; CLI-only)")
    subparsers.add_parser("bootstrap", help="Create the three Sheet tabs")
    subparsers.add_parser("run", help="One scheduled cycle")

    p_extract = subparsers.add_parser("extract", help="Run extractor on one source")
    p_extract.add_argument("--message-id", help="Gmail message id (live)")
    p_extract.add_argument("--file", help="Local body text file (offline)")

    p_backfill = subparsers.add_parser("backfill", help="Historical seed")
    p_backfill.add_argument("--dry-run", action="store_true")
    p_backfill.add_argument("--use-local", action="store_true", help="Read posts from data/raw/ instead of live scraping")

    subparsers.add_parser("test-notify", help="Fire test payload on all 3 channels")
    subparsers.add_parser("install-service", help="Install launchd plist")
    subparsers.add_parser("uninstall-service", help="Uninstall launchd plist")
    subparsers.add_parser("status", help="Show service status")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.command == "auth":
        return cmd_auth()
    if args.command == "bootstrap":
        return cmd_bootstrap()
    if args.command == "run":
        return cmd_run()
    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "backfill":
        return cmd_backfill(args)
    if args.command == "test-notify":
        return cmd_test_notify()
    if args.command == "install-service":
        return cmd_install_service()
    if args.command == "uninstall-service":
        return cmd_uninstall_service()
    if args.command == "status":
        return cmd_status()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
