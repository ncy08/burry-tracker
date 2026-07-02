"""Shared Gemini call wrapper for the two-stage Burry signal extractor.

Both Stage 1 (`substack_trader.extractor`) and Stage 2
(`substack_trader.critic`) call Gemini with the same structured-output
shape and the same 3-attempt exponential backoff. They differ only in
their system prompt, response schema, and log label, so the call shape
lives here in one place rather than being duplicated across both modules.

"""

from __future__ import annotations

import logging
import time
from typing import Any

from google.genai import errors as genai_errors
from google.genai import types

logger = logging.getLogger(__name__)


def generate_with_retry(
    client: Any,
    model: str,
    body_text: str,
    *,
    system_instruction: str,
    response_schema: dict,
    log_label: str,
) -> str:
    """Call Gemini with a 3-attempt exponential backoff (2s/4s/8s).

    `system_instruction`, `response_schema`, and `log_label` are the only
    per-caller differences between the Stage 1 extractor and Stage 2
    critic. `log_label` prefixes the transient/failure log lines (e.g.
    "Gemini" or "Critic Gemini").
    """
    delays = (2, 4, 8)
    last_exc: Exception | None = None
    for i, delay in enumerate(delays):
        try:
            response = client.models.generate_content(
                model=model,
                contents=body_text,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                ),
            )
            return response.text
        except (TimeoutError, genai_errors.APIError) as exc:
            last_exc = exc
            if i < len(delays) - 1:
                logger.warning(
                    "%s transient error (attempt %d): %s; sleeping %ds",
                    log_label,
                    i + 1,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    log_label,
                    len(delays),
                    exc,
                )
    assert last_exc is not None
    raise last_exc
