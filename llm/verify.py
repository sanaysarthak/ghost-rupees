"""
The verification gate every LLM output must survive before the
deterministic engine will trust it. Pure Python, no API calls, no
dependency on anthropic/pydantic - fully unit-testable offline, and
imported by core-adjacent code without pulling in the SDK.

Three checks, in order:
1. Hallucination guard - a UTR the model claims to have read must
   actually appear (post-normalisation) in the source string. This is
   the single most important line in this file: a model can produce a
   perfectly plausible-looking UTR that was never in the input, and
   nothing about a normal JSON-schema-valid response distinguishes
   that from a real extraction. Only a check against the raw source
   catches it.
2. Binding guard - that UTR must not already be claimed by a different
   credit (prevents one hallucinated/misread UTR from silently
   stealing another credit's match).
3. Identity guard - a claimed counterparty must resolve to a known
   client; if it doesn't, the field is cleared and marked UNRESOLVED
   rather than left as an unverified guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable


@dataclass(frozen=True, slots=True)
class ParsedNarration:
    """
    The internal, pure-Python representation of a parsed bank narration.
    llm/narration.py is responsible for converting whatever the Anthropic
    SDK's Pydantic output schema returns into one of these immediately
    after the API call, so this module stays free of any SDK/Pydantic
    dependency and is trivially unit-testable offline.
    """
    counterparty: str | None
    utr: str | None
    rail: str
    reference: str | None
    counterparty_status: str = "RESOLVED"


def normalise_for_search(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def gate_narration(
    raw: str,
    parsed: ParsedNarration,
    *,
    is_utr_already_bound: Callable[[str], bool] = lambda utr: False,
    resolve_client: Callable[[str], bool] = lambda name: True,
) -> "ParsedNarration | None":
    """
    Returns the (possibly amended) parsed object, or None if the
    extraction must be discarded outright (the hallucination or
    binding guard failed).
    """
    if parsed.utr:
        norm_raw = normalise_for_search(raw)
        norm_utr = normalise_for_search(parsed.utr)
        if norm_utr not in norm_raw:
            return None  # hallucination guard: not discardable, not trustable
        if is_utr_already_bound(norm_utr):
            return None  # binding guard

    if parsed.counterparty and not resolve_client(parsed.counterparty):
        parsed = replace(parsed, counterparty=None, counterparty_status="UNRESOLVED")

    return parsed


class VerificationStats:
    """Tracks the gate's discard rate across a batch - the number the
    README reports as evidence the gate is real, not decorative."""

    def __init__(self) -> None:
        self.total = 0
        self.discarded = 0

    def record(self, discarded: bool) -> None:
        self.total += 1
        if discarded:
            self.discarded += 1

    @property
    def discard_rate_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * self.discarded / self.total, 2)
