"""Stage 2 of the Burry signal extractor: the critic.

Receives a candidate produced by `substack_trader.extractor` (Stage 1)
and decides whether to confirm or veto it. The critic re-reads the
candidate against six Burry-domain clauses derived from the extraction
audit:

1. "I continue to own X" / "I personally own X" maps to a
   PositionDisclosureSignal, NOT a new ExecutionSignal.
2. "I am not selling these today" maps to a HoldConfirmSignal, NOT a
   new buy.
3. "I did not add to X today" is an explicit negation. If Stage 1
   emitted it as an EXECUTION with assertion_mode=PRESENT, veto.
4. "I rolled my X puts" should produce two paired candidates (a
   CLOSURE and a new EXECUTION). A single buy or sell from a roll is a
   veto.
5. "Tomorrow I will take a position in X" maps to a FuturePlanSignal,
   NOT a new ExecutionSignal.
6. "I am a buyer in the seven dollar range" maps to a
   ConditionalSignal, NOT a new ExecutionSignal.

The critic returns a binary verdict (`confirm` or `veto`) with a
one-line reason. Stage 1 is the only place where misclassifications
can be repaired; the critic only removes false positives.

"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from substack_trader.gemini_client import get_client
from substack_trader.config import Config
from substack_trader.gemini_retry import generate_with_retry
from substack_trader.signals import Signal

CriticVerdictLiteral = Literal["confirm", "veto"]


@dataclass
class CriticVerdict:
    verdict: CriticVerdictLiteral
    reason: str


CRITIC_SYSTEM_PROMPT = """\
You are the Stage 2 critic for a Michael Burry signal extractor. \
Stage 1 emitted a candidate signal from a Burry Substack post; your \
job is to re-read the candidate against six domain clauses and either \
CONFIRM the candidate or VETO it. You cannot change the candidate's \
signal_type; you can only decide whether to keep or drop it.

VETO RULES (apply in order; the first matching rule wins):

1. RE-CONFIRMATION AS EXECUTION. If the candidate is signal_type \
EXECUTION and the surrounding paragraph uses re-confirmation language \
("I continue to own X", "I personally own X", "I still hold X", "I am \
still long Y"), VETO. The correct type is POSITION_DISCLOSURE or \
HOLD_CONFIRM.

2. NOT-SELLING AS EXECUTION. If the candidate is signal_type EXECUTION \
and the surrounding paragraph says the author is NOT selling ("I am \
not selling these today", "I did not sell"), VETO. The correct type is \
HOLD_CONFIRM.

3. NEGATION AS PRESENT EXECUTION. If the candidate is signal_type \
EXECUTION with assertion_mode=PRESENT and the surrounding paragraph \
contains an explicit negation of the action ("I did not add to X \
today", "I have not bought X yet"), VETO. The candidate should have \
been emitted with assertion_mode=ABSENT, not PRESENT.

4. ROLLED OPTIONS AS SINGLE TRADE. If the surrounding paragraph \
describes rolling options ("I rolled my X puts", "I rolled the QQQ \
puts forward") and the candidate is a single EXECUTION (not paired \
with a CLOSURE), VETO. Rolls must produce two candidates; a lone roll \
candidate is incomplete.

5. FUTURE PLAN AS EXECUTION. If the candidate is signal_type EXECUTION \
and the surrounding paragraph uses future-tense framing ("tomorrow I \
will take a position", "next week I plan to buy", "I will be adding"), \
VETO. The correct type is FUTURE_PLAN.

6. CONDITIONAL AS EXECUTION. If the candidate is signal_type EXECUTION \
and the surrounding paragraph is conditional ("I am a buyer in the \
seven dollar range", "if it drops below 100 I will buy", "I would buy \
on weakness"), VETO. The correct type is CONDITIONAL.

If none of the above veto rules apply, CONFIRM.

ROLLED OPTIONS CONFIRMATION: a CLOSURE candidate from a roll \
("I rolled my X puts") is CONFIRMED. A new EXECUTION from the roll is \
CONFIRMED only when the candidate explicitly references the new \
contract (different strike or expiration). The veto in rule 4 targets \
single-EXECUTION candidates that omit the closure.

OUTPUT:
Return the JSON object {"verdict": "confirm"|"veto", "reason": "<one \
short sentence>"}. The reason must cite which rule applied (e.g., \
"rule 1: re-confirmation language") or "no veto rule matched" for \
confirms.
"""


CRITIC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["confirm", "veto"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
}


def _candidate_summary(signal: Signal, candidate: dict) -> str:
    """Render the critic's per-candidate input.

    Sends the signal_type, ticker, evidence_text, assertion_mode, the
    Stage 1 rationale, and the paragraph_context. Signal-specific
    fields are serialized via Pydantic so the critic sees the full
    typed payload (direction, weight_hint, condition_text, etc.).
    """
    signal_dump = signal.model_dump(mode="json")
    payload = {
        "signal_type": signal_dump.get("signal_type"),
        "ticker": signal_dump.get("ticker"),
        "assertion_mode": signal_dump.get("assertion_mode"),
        "evidence_text": signal_dump.get("evidence_text"),
        "paragraph_context": candidate.get("paragraph_context", ""),
        "stage1_rationale": candidate.get("rationale", ""),
        "extra_fields": {
            k: v
            for k, v in signal_dump.items()
            if k
            not in {
                "signal_type",
                "ticker",
                "assertion_mode",
                "evidence_text",
                "confidence",
                "source_post_url",
                "valid_time",
                "transaction_time",
            }
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def vet_candidate(
    signal: Signal, candidate: dict, config: Config
) -> CriticVerdict:
    """Run Stage 2 on a single (Signal, candidate) pair.

    Returns a `CriticVerdict` with `verdict` of "confirm" or "veto" and
    a one-line `reason`. Raises on JSON parsing or transient API
    failures that exhausted retries.
    """
    body_text = _candidate_summary(signal, candidate)
    client = get_client()
    raw = generate_with_retry(
        client,
        config.extraction_model,
        body_text,
        system_instruction=CRITIC_SYSTEM_PROMPT,
        response_schema=CRITIC_SCHEMA,
        log_label="Critic Gemini",
    )
    data = json.loads(raw)
    return CriticVerdict(verdict=data["verdict"], reason=data["reason"])


def filter_candidates(
    candidates: list[tuple[Signal, dict]], config: Config
) -> list[tuple[Signal, dict, CriticVerdict]]:
    """Run the critic over every (Signal, candidate) pair.

    Returns triples (Signal, candidate_dict, CriticVerdict). Callers
    decide what to do with vetoed entries (typically log and discard).
    """
    out: list[tuple[Signal, dict, CriticVerdict]] = []
    for signal, candidate in candidates:
        verdict = vet_candidate(signal, candidate, config)
        out.append((signal, candidate, verdict))
    return out
