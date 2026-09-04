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
"""

from __future__ import annotations

import re

from llm.client import MODEL, get_client
from llm.verify import ParsedNarration, VerificationStats, gate_narration

_SYSTEM_PROMPT = """You extract structured fields from a single Indian bank or \
payment-gateway credit narration line. Narrations are short, inconsistent, and \
often abbreviated (UPI/NEFT/IMPS/RTGS formats, bank codes, truncated names).

Extract exactly:
- counterparty: the payer's name as it appears, cleaned up to a readable form \
  (e.g. "ARJUNTEXTILESPVT" -> "Arjun Textiles Pvt"). Null if not identifiable.
- utr: the UTR/reference number exactly as it appears in the text - copy it \
  character for character, do not reformat or guess digits. Null if absent.
- rail: one of UPI, IMPS, NEFT, RTGS, CARD, WIRE, based on what the narration \
  indicates. Default to UPI if genuinely ambiguous.
- reference: any invoice/receipt reference embedded in the narration (e.g. \
  "inv-014", "part1"). Null if absent.

If you are not confident a field is present in the text, return null for it \
rather than guessing. Do not invent a UTR or counterparty that is not visibly \
present in the input."""

_DIGIT_RUN_RE = re.compile(r"\b\d{6,22}\b")


def parse_narration_stub(raw: str) -> ParsedNarration:
    """The non-LLM baseline: a bare digit-run regex for the UTR, nothing else."""
    m = _DIGIT_RUN_RE.search(raw)
    return ParsedNarration(counterparty=None, utr=m.group(0) if m else None, rail="UPI", reference=None)


def parse_narration(raw: str, *, client=None) -> ParsedNarration:
    """Single narration -> ParsedNarration via a live Claude call. Raises
    llm.client.LLMNotConfigured if no credentials are available."""
    from pydantic import BaseModel

    class _NarrationSchema(BaseModel):
        counterparty: str | None
        utr: str | None
        rail: str
        reference: str | None

    client = client or get_client()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": raw}],
        output_format=_NarrationSchema,
    )
    p = response.parsed_output
    return ParsedNarration(counterparty=p.counterparty, utr=p.utr, rail=p.rail, reference=p.reference)


def parse_narration_verified(raw: str, *, client=None, is_utr_already_bound=lambda u: False,
                              resolve_client=lambda n: True, stats: VerificationStats | None = None
                              ) -> ParsedNarration | None:
    """The full job-1 pipeline: parse, then gate. Returns None if discarded."""
    parsed = parse_narration(raw, client=client)
    gated = gate_narration(raw, parsed, is_utr_already_bound=is_utr_already_bound, resolve_client=resolve_client)
    if stats is not None:
        stats.record(discarded=gated is None)
    return gated
