"""Stage 1 of the Burry signal extractor.

High-recall extraction of candidate signals from a Burry Substack post.
Emits a flat list of dicts (no Pydantic discriminated union, since Gemini
does not consistently support unions in `response_schema`) plus a
one-line rationale and a paragraph-context window for the Stage 2
critic in `substack_trader.critic`.

Bitemporal fields (`valid_time`, `transaction_time`) and `source_post_url`
are filled by this module after the LLM call. The LLM cannot reliably
know ingest time, and the post URL is metadata.

Hard cap: twenty candidate signals per post. The Stage 1 prompt states
the cap; the parser also enforces it in code.

"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from pydantic import ValidationError

from substack_trader.gemini_client import get_client
from substack_trader.config import Config
from substack_trader.gemini_retry import generate_with_retry
from substack_trader.signals import SIGNAL_CLASSES, Signal, SignalAdapter

logger = logging.getLogger(__name__)

MAX_SIGNALS_PER_POST = 20

SIGNAL_TYPES = [
    "EXECUTION",
    "POSITION_DISCLOSURE",
    "ALLOCATION_TARGET",
    "AGGREGATE_CAP",
    "HOLD_CONFIRM",
    "FUTURE_PLAN",
    "CONDITIONAL",
    "CLOSURE",
    "HYPOTHETICAL",
    "WATCHLIST",
]

ASSERTION_MODES = [
    "PRESENT",
    "ABSENT",
    "HYPOTHETICAL",
    "CONDITIONAL",
    "POSSIBLE",
    "ASSOCIATED_WITH_OTHER",
]


SYSTEM_PROMPT = """\
You read Michael Burry's Substack posts ("Cassandra Unchained") and \
emit candidate trading signals. You are Stage 1 of a two-stage \
pipeline. A critic in Stage 2 re-reads each candidate, so favor recall \
over precision. When uncertain, emit the candidate and let the critic \
decide.

Each candidate is a JSON object with a `signal_type` discriminator and \
the fields appropriate for that type. The discriminator MUST be one of \
these literal SCREAMING_SNAKE_CASE strings: EXECUTION, \
POSITION_DISCLOSURE, ALLOCATION_TARGET, AGGREGATE_CAP, HOLD_CONFIRM, \
FUTURE_PLAN, CONDITIONAL, CLOSURE, HYPOTHETICAL, WATCHLIST.

WHAT EACH SIGNAL TYPE MEANS:
- EXECUTION: a confirmed past trade ("today I bought GME"). Required \
extra fields: direction (buy/sell), instrument_type (stock/call/put/other). \
Optional: quantity, strike, expiration, fill_price.
- POSITION_DISCLOSURE: a holding statement, often with a stated weight, \
no new action ("PYPL is now a full position at 6.6% of the portfolio", \
"I personally own MSCI"). Required: instrument_type. Optional: \
weight_hint as percent (e.g., 6.6 for 6.6%).
- ALLOCATION_TARGET: a stated target weight that is not a current \
holding ("I want NVDA to be 5% of the book"). Required: target_pct.
- AGGREGATE_CAP: a constraint over a group of positions ("puts are \
about 6.9% of my portfolio", "no position above 5%"). Ticker is \
typically null. Required: grouping (e.g., "puts", "options"), cap_pct.
- HOLD_CONFIRM: an explicit re-confirmation of an existing position \
without a new action ("I continue to own MSCI", "I am not selling \
these today"). No extra required fields.
- FUTURE_PLAN: a stated future action ("tomorrow I will take a \
position in X"). Required: intent_summary. Optional: timing_hint.
- CONDITIONAL: an action contingent on an event ("I am a buyer in the \
seven dollar range", "if it drops below 100 I will buy"). Required: \
condition_text. Optional: intended_direction (buy/sell).
- CLOSURE: a position exit ("I closed my SOXX puts"). Optional: \
reason_hint.
- HYPOTHETICAL: an analytical or speculative mention not tied to \
action. Optional: framing.
- WATCHLIST: monitoring without action ("I am watching MOH").

DO NOT EXTRACT:
- Analytical commentary about a position ("I am bullish on X", "X is \
undervalued").
- Questions about positions ("would X be a buy here?").
- Statements that explain past trade rationale from prior years ("I \
bought X back in 2019").
- Statements about other people's trades ("the market is buying X").
- Hypothetical or conditional scenarios as EXECUTION; classify them as \
HYPOTHETICAL or CONDITIONAL instead.
- Position re-confirmations as new buys ("I am still long Y") — these \
are HOLD_CONFIRM, not EXECUTION.
- Names added to research coverage or an analysis list ("I added four \
names", "added to the coverage"). "Added" is an EXECUTION buy ONLY when \
the object is a position, portfolio, book, or holding ("I added to my \
PYPL position", "I added 3% to the book"). When the object is a research \
list or a set of names under analysis, emit WATCHLIST or skip; do NOT \
emit EXECUTION. The count of names is irrelevant; the destination decides.

ASSERTION MODE:
- PRESENT: the default; an actual statement ("I bought GME").
- ABSENT: an explicit negation ("I did not add to X today"). Set the \
appropriate signal_type and assertion_mode=ABSENT; the critic uses \
this to keep negations out of the replay.
- HYPOTHETICAL: a "what if" framing.
- CONDITIONAL: an "if X then Y" framing.
- POSSIBLE: hedged language ("I may buy more").
- ASSOCIATED_WITH_OTHER: language attributing the action to a third \
party.

EVIDENCE AND RATIONALE:
- evidence_text: a verbatim or near-verbatim quote that supports the \
candidate. Never empty.
- paragraph_context: the full sentence or short paragraph containing \
the evidence. Stage 2 needs this to apply its veto rules; do not \
truncate to a fragment.
- rationale: one short sentence explaining why this candidate matches \
the chosen signal_type.
- confidence: "high" when the language is unambiguous; "medium" when a \
required field is inferred from context; "low" when the candidate is \
ambiguous.

OPTIONS:
- Resolve month-and-year phrasing to the third Friday of that month \
(e.g., "January 2027 puts" -> expiration "2027-01-15", "March 2027 \
puts" -> "2027-03-19"). Set expiration=null only when the post is \
genuinely ambiguous about which month is meant.

ROLLED OPTIONS:
- "I rolled my X puts" emits TWO candidates: a CLOSURE on the old \
contract and an EXECUTION on the new contract. Do not collapse to a \
single buy or sell.
- "SELL X & BUY Y ... @ net debit" (an explicit paired roll) emits TWO \
EXECUTIONs: a sell EXECUTION on the old contract and a buy EXECUTION on \
the new contract. Always emit the sell leg; never keep only the buy.

SELLS:
- An outright disposal ("I sold my position in MSFT", "I sold MSFT \
today") is an EXECUTION with direction=sell. Give sells the same recall \
as buys; do not under-emit them.

TICKER:
- uppercase root symbol only ("QQQ", "NVDA", "MSFT").
- For AGGREGATE_CAP, ticker may be null.

LISTING AND CURRENCY:
- company_name: the full name when stated ("Meituan", "Haidilao", \
"Temple & Webster").
- exchange: the listing venue when identifiable (US, HK, ASX, or other). \
HK numeric tickers ("3690", "6862") are HK; "TPW AU" is ASX.
- currency: the price currency (USD, HKD, AUD, ...). "HKD 468" sets \
fill_price=468 and currency=HKD; "AUD ~5.40" sets fill_price=5.40 and \
currency=AUD. Default currency=USD when unstated.

HARD CAP: At most twenty candidates per post. If you find more, keep \
the highest-confidence twenty.

OUTPUT:
Return the JSON object {"signals": [Candidate, ...]}. Empty array if \
no candidates. Do NOT include any text outside the JSON.
"""


EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signal_type": {"type": "string", "enum": SIGNAL_TYPES},
                    "ticker": {"type": "string", "nullable": True},
                    "evidence_text": {"type": "string"},
                    "paragraph_context": {"type": "string"},
                    "rationale": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "assertion_mode": {"type": "string", "enum": ASSERTION_MODES},
                    "direction": {
                        "type": "string",
                        "enum": ["buy", "sell"],
                        "nullable": True,
                    },
                    "instrument_type": {
                        "type": "string",
                        "enum": ["stock", "call", "put", "other"],
                        "nullable": True,
                    },
                    "quantity": {"type": "integer", "nullable": True},
                    "strike": {"type": "number", "nullable": True},
                    "expiration": {"type": "string", "nullable": True},
                    "fill_price": {"type": "number", "nullable": True},
                    "company_name": {"type": "string", "nullable": True},
                    "exchange": {"type": "string", "nullable": True},
                    "currency": {"type": "string", "nullable": True},
                    "weight_hint": {"type": "number", "nullable": True},
                    "target_pct": {"type": "number", "nullable": True},
                    "grouping": {"type": "string", "nullable": True},
                    "cap_pct": {"type": "number", "nullable": True},
                    "timing_hint": {"type": "string", "nullable": True},
                    "intent_summary": {"type": "string", "nullable": True},
                    "condition_text": {"type": "string", "nullable": True},
                    "intended_direction": {
                        "type": "string",
                        "enum": ["buy", "sell"],
                        "nullable": True,
                    },
                    "reason_hint": {"type": "string", "nullable": True},
                    "framing": {"type": "string", "nullable": True},
                },
                "required": [
                    "signal_type",
                    "evidence_text",
                    "paragraph_context",
                    "rationale",
                    "confidence",
                ],
            },
        }
    },
    "required": ["signals"],
}


def parse_candidates(
    raw_json: str,
    *,
    source_post_url: str,
    valid_time: datetime,
    transaction_time: datetime,
) -> list[tuple[Signal, dict]]:
    """Parse Stage 1 JSON output into validated (Signal, candidate) pairs.

    The candidate dict carries Stage-1-only fields (`rationale`,
    `paragraph_context`) that the critic needs but `Signal` does not
    store. The returned list is capped at MAX_SIGNALS_PER_POST.

    The flat Gemini response schema lets the LLM attach option fields
    (e.g., `quantity`, `expiration`) to a signal_type whose model does
    not declare them; those off-type fields are dropped (and logged)
    rather than crashing the post. A single signal that still fails
    validation (missing required field, unknown discriminator, bad enum)
    is skipped with a warning so one bad candidate never discards the
    whole post's signals. JSON-level breakage (an unparseable response)
    still raises so genuine breakage fails loudly.
    """
    data = json.loads(raw_json)
    raw_signals = data.get("signals", [])[:MAX_SIGNALS_PER_POST]
    out: list[tuple[Signal, dict]] = []
    for cand in raw_signals:
        signal_payload = {
            k: v
            for k, v in cand.items()
            if k not in {"rationale", "paragraph_context"}
        }
        if signal_payload.get("ticker"):
            signal_payload["ticker"] = signal_payload["ticker"].upper()
        signal_payload["source_post_url"] = source_post_url
        signal_payload["valid_time"] = valid_time
        signal_payload["transaction_time"] = transaction_time
        signal_payload.setdefault("assertion_mode", "PRESENT")
        model_cls = SIGNAL_CLASSES.get(signal_payload.get("signal_type"))
        if model_cls is not None:
            off_type = [k for k in signal_payload if k not in model_cls.model_fields]
            if off_type:
                logger.info(
                    "Stage 1 %s: dropping off-type field(s) %s emitted by the LLM",
                    signal_payload["signal_type"],
                    sorted(off_type),
                )
                for k in off_type:
                    del signal_payload[k]
        # A single malformed signal (missing required field, unknown
        # discriminator, bad enum) is LLM noise. Skip it with a warning and
        # keep the rest of the post's signals; one bad candidate must not
        # discard a whole post. JSON-level breakage above still raises.
        try:
            signal = SignalAdapter.validate_python(signal_payload)
        except ValidationError as exc:
            logger.warning(
                "Stage 1: skipping malformed %s signal: %s",
                signal_payload.get("signal_type"),
                str(exc).splitlines()[0],
            )
            continue
        out.append((signal, cand))
    return out


def extract_candidates(
    body_text: str,
    config: Config,
    *,
    source_post_url: str,
    valid_time: datetime,
    transaction_time: datetime | None = None,
) -> list[tuple[Signal, dict]]:
    """Stage 1 entry point: extract candidate Signals from a post body.

    Returns (Signal, candidate_dict) pairs ready for the critic in
    `substack_trader.critic`. Returns [] for empty bodies. Raises on
    hard failures (non-transient API errors, schema mismatch, malformed
    JSON, discriminator/validation errors).
    """
    if not body_text.strip():
        return []
    if transaction_time is None:
        transaction_time = datetime.now(timezone.utc).replace(tzinfo=None)
    client = get_client()
    raw = generate_with_retry(
        client,
        config.extraction_model,
        body_text,
        system_instruction=SYSTEM_PROMPT,
        response_schema=EXTRACTION_SCHEMA,
        log_label="Gemini",
    )
    return parse_candidates(
        raw,
        source_post_url=source_post_url,
        valid_time=valid_time,
        transaction_time=transaction_time,
    )
