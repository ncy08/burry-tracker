"""OAuth bootstrap and headless client construction.

Strict separation between two execution contexts:

- bootstrap_oauth_interactive: CLI-only. May spawn a browser. Never called
  from any code path reachable by launchd.
- load_clients_headless: used by every scheduled / non-interactive entry
  point. Refresh-only; raises RuntimeError if the token is missing or
  scopes are insufficient. Never spawns a browser.

The launchd subprocess has no GUI session to host the consent flow,
so any browser spawn from a daemon path is a defect.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from substack_trader.config import Config

if TYPE_CHECKING:
    import gspread
    from googleapiclient.discovery import Resource

logger = logging.getLogger(__name__)


def bootstrap_oauth_interactive(config: Config) -> None:
    """Run the OAuth consent flow and persist the token.

    CLI-only entry point. Spawns a browser via run_local_server. NEVER call
    this from any code path reachable by launchd.

    Credential source order:
      1. GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET env vars (preferred)
      2. client_secrets.json file at config.client_secrets_path (fallback)

    Raises:
        FileNotFoundError: neither env vars nor client_secrets.json available.
    """
    import os

    from google_auth_oauthlib.flow import InstalledAppFlow

    config.state_dir.mkdir(parents=True, exist_ok=True)

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

    if client_id and client_secret:
        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, list(config.oauth_scopes))
    elif config.client_secrets_path.exists():
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config.client_secrets_path), list(config.oauth_scopes)
        )
    else:
        raise FileNotFoundError(
            "OAuth credentials missing. Either:\n"
            "  (a) Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env\n"
            f"  (b) Download client_secrets.json to {config.client_secrets_path}\n"
            "      from GCP Console: APIs & Services -> Credentials ->\n"
            "      OAuth client ID -> Desktop app -> Download JSON"
        )

    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    config.oauth_token_path.write_text(creds.to_json())
    config.oauth_token_path.chmod(0o600)

    print(f"OAuth token written to {config.oauth_token_path}")
    print(f"Granted scopes: {', '.join(creds.scopes or [])}")


def _emit_reauth_sms(config: Config) -> None:
    """Fire a single Twilio SMS when the refresh token is dead.

    Gmail is unavailable in this state (the refresh just failed), so we cannot
    use the GmailDraftNotifier. macOS banner is unreliable for unattended
    sessions. SMS is the only signal that reliably reaches the user.
    """
    if not (
        config.twilio_account_sid
        and config.twilio_auth_token
        and config.twilio_from_number
        and config.twilio_to_number
    ):
        logger.error(
            "Substack trader: re-auth required, but Twilio is unconfigured. "
            "Run: python -m substack_trader auth"
        )
        return
    try:
        from twilio.rest import Client as TwilioClient

        client = TwilioClient(config.twilio_account_sid, config.twilio_auth_token)
        client.messages.create(
            body="Substack trader: re-auth required. Run: python -m substack_trader auth",
            from_=config.twilio_from_number,
            to=config.twilio_to_number,
        )
    except Exception as exc:
        logger.error("Failed to send re-auth SMS: %s", exc)


def load_clients_headless(config: Config) -> tuple[Resource, gspread.Client]:
    """Load OAuth credentials and build (gmail_service, sheets_client).

    Used by every entry point reachable from launchd. Refresh-only:
    never spawns a browser. Raises RuntimeError on any unrecoverable
    state so the daemon fails loudly rather than silently looping.
    """
    import gspread
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not config.oauth_token_path.exists():
        raise RuntimeError(
            f"OAuth token missing at {config.oauth_token_path}. "
            "Run 'python -m substack_trader auth' first."
        )

    creds = Credentials.from_authorized_user_file(
        str(config.oauth_token_path), list(config.oauth_scopes)
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            config.oauth_token_path.write_text(creds.to_json())
        except RefreshError:
            _emit_reauth_sms(config)
            raise

    granted = set(creds.scopes or [])
    required = set(config.oauth_scopes)
    missing = required - granted
    if missing:
        raise RuntimeError(
            f"Token missing scopes: {sorted(missing)}. "
            "Re-run 'python -m substack_trader auth'."
        )

    gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sheets_client = gspread.authorize(creds)
    return gmail_service, sheets_client
