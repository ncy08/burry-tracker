"""Configuration for the Substack trader pipeline.

Loaded from .env at the repo root via python-dotenv. Keep the @dataclass body
free of side effects so it stays mockable in tests; the state-directory mkdir
happens inside Config.load(), not __post_init__.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/spreadsheets",
)


def load_env() -> None:
    """Load .env from repo root."""
    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        logger.warning("No .env file found at %s", env_path)


def _float_env(key: str, default: float) -> float:
    """Read a float env var, falling back to `default` on missing or garbage."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Env %s=%r is not a float; using default %s", key, raw, default)
        return default


def _opt_path_env(key: str) -> Path | None:
    """Read an optional path env var; None when unset so `load` can derive it."""
    raw = os.environ.get(key)
    return Path(raw) if raw and raw.strip() else None


@dataclass
class Config:
    state_dir: Path = field(default_factory=lambda: Path.home() / ".substack-trader")
    oauth_token_path: Path = field(
        default_factory=lambda: Path.home() / ".substack-trader" / "token.json"
    )
    # Downloaded once from GCP Console: APIs & Services -> Credentials ->
    # OAuth client ID -> Desktop app -> Download JSON. Without this file,
    # bootstrap_oauth_interactive raises FileNotFoundError with the path.
    client_secrets_path: Path = field(
        default_factory=lambda: Path.home() / ".substack-trader" / "client_secrets.json"
    )

    # Sheet ID lookup tries BURRY_TRACKER first (preferred name in .env),
    # then SUBSTACK_TRADER_SHEET_ID for backwards compatibility.
    sheet_id: str = field(
        default_factory=lambda: os.environ.get("BURRY_TRACKER")
        or os.environ.get("SUBSTACK_TRADER_SHEET_ID", "")
    )
    gmail_label: str = field(
        default_factory=lambda: os.environ.get("SUBSTACK_GMAIL_LABEL", "trading-alerts")
    )
    # Verified actual sender domain on first install. michaeljburry@substack.com
    # is the most common Substack sender pattern; override via env if different.
    gmail_sender_filter: str = field(
        default_factory=lambda: os.environ.get(
            "SUBSTACK_GMAIL_SENDER", "michaeljburry@substack.com"
        )
    )

    extraction_model: str = "gemini-3.1-pro-preview"

    # Rebalance engine (Phase 5). risk_multiplier scales every Burry weight
    # before computing user-side dollars (1.0 mirrors Burry one-to-one). user_portfolio_csv_path stays None here and is derived from
    # state_dir inside load() when the env var is unset.
    risk_multiplier: float = field(
        default_factory=lambda: _float_env("RISK_MULTIPLIER", 1.0)
    )
    user_portfolio_csv_path: Path | None = field(
        default_factory=lambda: _opt_path_env("USER_PORTFOLIO_CSV_PATH")
    )
    min_rebalance_usd: float = field(
        default_factory=lambda: _float_env("MIN_REBALANCE_USD", 100.0)
    )

    twilio_account_sid: str = field(
        default_factory=lambda: os.environ.get("TWILIO_ACCOUNT_SID", "")
    )
    twilio_auth_token: str = field(
        default_factory=lambda: os.environ.get("TWILIO_AUTH_TOKEN", "")
    )
    twilio_from_number: str = field(
        default_factory=lambda: os.environ.get("TWILIO_FROM_NUMBER", "")
    )
    twilio_to_number: str = field(
        default_factory=lambda: os.environ.get("TWILIO_TO_NUMBER", "")
    )

    oauth_scopes: tuple[str, ...] = OAUTH_SCOPES

    tz: str = "America/New_York"

    @classmethod
    def load(cls) -> Config:
        """Load config from .env and ensure the state dir exists.

        Eager mkdir lives here (not __post_init__) so test fixtures can
        instantiate Config() without touching the filesystem.
        """
        load_env()
        cfg = cls()
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        if cfg.user_portfolio_csv_path is None:
            cfg.user_portfolio_csv_path = cfg.state_dir / "user_portfolio.csv"
        return cfg
