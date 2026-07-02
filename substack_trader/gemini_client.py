"""Gemini API client construction for the two-stage extractor.

Centralizes API key resolution and client creation so the Stage 1
extractor and Stage 2 critic share one code path.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load .env from the repo root regardless of working directory.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def get_api_key() -> str:
    """Resolve the Gemini API key from either supported env var name."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            "Gemini API key not found. Set GEMINI_API_KEY or GOOGLE_API_KEY in .env."
        )
    return api_key


def get_client(timeout: int | None = None) -> genai.Client:
    """Create a Gemini client with an optional request timeout (ms)."""
    kwargs: dict = {"api_key": get_api_key()}
    if timeout is not None:
        kwargs["http_options"] = types.HttpOptions(timeout=timeout)
    return genai.Client(**kwargs)
