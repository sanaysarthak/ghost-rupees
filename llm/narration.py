"""
Job 1: bank/gateway narration -> structured fields.

UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01
  -> counterparty="ARJUN TEXTILES", utr="452118839021", rail="UPI", reference="inv-ghost-01"

Every result is passed through llm.verify.gate_narration before it is
trusted - see that module's docstring for why. `parse_narration_stub`
is the deliberately-dumb non-LLM baseline used as the "off" condition
in the ablation study (plan/baaki.md §8): a generic digit-run regex,
nothing more. It is not a fake LLM - it is what the matcher falls back
to identity/UTR-only matching without narration understanding, and the
gap between its match rate and the real parser's is the evidence that
the model is load-bearing.

`known_counterparties`, discovered live on 2026-09-05: the first real
(non-mocked) run against Gemini showed that WITHOUT a reference list,
the model correctly declines to guess a specific unlisted expansion of
a garbled name - "BLUPEAK CNSLTNG" comes back as "Blupeak Cnsltng"
(title-cased, not expanded), which is honest but doesn't help
core.match's tier-2 tie-break, since exact-token-set matching against
"BluePeak Consulting" then fails on "blupeak" != "bluepeak". This is
not a hallucination risk to guard against - it is a missing-context
problem. A real reconciliation tool always has the user's own client
roster to check against (that's what a human does too - "does this
narration look like it's from someone on my client list?"), so
`parse_narration` now accepts one and asks the model to match against
it, falling back to a cleaned-up literal reading when nothing plausibly
matches.
"""

from __future__ import annotations

import re

from llm.client import MODEL, call_with_retry, get_client
from llm.verify import ParsedNarration, VerificationStats, gate_narration

_SYSTEM_PROMPT = """You extract structured fields from a single Indian bank or \
payment-gateway credit narration line. Narrations are short, inconsistent, and \
often abbreviated (UPI/NEFT/IMPS/RTGS formats, bank codes, truncated names).

Extract exactly:
- counterparty: the payer's name. If a list of known counterparties is given \
  below and the narration plausibly refers to one of them (even through an \
  abbreviation, dropped vowels, or a typo - e.g. "BLUPEAK CNSLTNG" for \
  "BluePeak Consulting"), return that counterparty's name EXACTLY as given in \
  the list. Only if none of the known counterparties plausibly match, fall \
  back to your own cleaned-up reading of the text (e.g. "ARJUNTEXTILESPVT" -> \
  "Arjun Textiles Pvt"). Null if truly not identifiable.
- utr: the UTR/reference number exactly as it appears in the text - copy it \
  character for character, do not reformat or guess digits. Null if absent.
- rail: one of UPI, IMPS, NEFT, RTGS, CARD, WIRE, based on what the narration \
  indicates. Default to UPI if genuinely ambiguous.
- reference: any invoice/receipt reference embedded in the narration (e.g. \
  "inv-014", "part1"). Null if absent.

If you are not confident a field is present in the text, return null for it \
rather than guessing. Do not invent a UTR that is not visibly present in the \
input, and do not match a known counterparty unless the narration genuinely \
resembles their name - an unrelated narration should get your best literal \
reading, not a forced match."""

_DIGIT_RUN_RE = re.compile(r"\b\d{6,22}\b")


def parse_narration_stub(raw: str) -> ParsedNarration:
    """The non-LLM baseline: a bare digit-run regex for the UTR, nothing else."""
    m = _DIGIT_RUN_RE.search(raw)
    return ParsedNarration(counterparty=None, utr=m.group(0) if m else None, rail="UPI", reference=None)


def parse_narration(raw: str, *, client=None, known_counterparties: list[str] | None = None) -> ParsedNarration:
    """
    Single narration -> ParsedNarration via a live Gemini call. Raises
    llm.client.LLMNotConfigured if no credentials are available.

    known_counterparties: the caller's own client/counterparty roster
    (e.g. [c.name for c in batch.clients]), if available. Passed as
    plain strings, never imported types, so this stays a thin function
    boundary rather than coupling llm/ to core/'s data model.
    """
    from google.genai import types
    from pydantic import BaseModel

    class _NarrationSchema(BaseModel):
        counterparty: str | None
        utr: str | None
        rail: str
        reference: str | None

    client = client or get_client()
    user_content = raw
    if known_counterparties:
        roster = "\n".join(f"- {name}" for name in known_counterparties)
        user_content = f"Known counterparties:\n{roster}\n\nNarration:\n{raw}"

    response = call_with_retry(
        client.models.generate_content,
        model=MODEL,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_NarrationSchema,
        ),
    )
    p = response.parsed
    return ParsedNarration(counterparty=p.counterparty, utr=p.utr, rail=p.rail, reference=p.reference)


def parse_narration_verified(raw: str, *, client=None, known_counterparties: list[str] | None = None,
                              is_utr_already_bound=lambda u: False,
                              resolve_client=lambda n: True, stats: VerificationStats | None = None
                              ) -> ParsedNarration | None:
    """The full job-1 pipeline: parse, then gate. Returns None if discarded."""
    parsed = parse_narration(raw, client=client, known_counterparties=known_counterparties)
    gated = gate_narration(raw, parsed, is_utr_already_bound=is_utr_already_bound, resolve_client=resolve_client)
    if stats is not None:
        stats.record(discarded=gated is None)
    return gated
