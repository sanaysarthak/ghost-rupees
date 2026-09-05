"""
Job 3: exception -> human explanation + a ready-to-send chase message.

Prose only. Every number that appears in the model's output is
supplied to it as a already-computed fact (the exception's code,
amount, and explanation come from core.classify, never from the
model) - the prompt explicitly forbids introducing any figure that
was not given to it, and the deterministic amount is re-injected into
the final text programmatically rather than trusted from the model's
own restatement, so a transcription slip can't silently change a
rupee figure a user might act on.
"""

from __future__ import annotations

from core.classify import Exception_
from core.money import paisa_to_rupees_str
from llm.client import MODEL, call_with_retry, get_client

_SYSTEM_PROMPT = """You write a short, plain-English explanation and a polite \
follow-up chase message for a payment-reconciliation finding. You are given the \
exact facts (a code, a rupee amount, a deterministic explanation, and a \
recommended action) - your job is ONLY to phrase them clearly and politely, in \
first person, as if the freelancer/business owner is about to send the chase \
message themselves. Do NOT introduce any rupee amount, date, or fact that was \
not given to you. Do NOT soften or omit the specific finding."""


def build_narrative(exc: Exception_, *, client=None) -> dict:
    """Returns {"narrative": str, "chase_message": str}. Raises
    llm.client.LLMNotConfigured if no credentials are available."""
    from google.genai import types
    from pydantic import BaseModel

    class _NarrativeSchema(BaseModel):
        narrative: str
        chase_message: str

    client = client or get_client()
    amount_str = paisa_to_rupees_str(exc.amount_paisa)
    user_content = (
        f"Code: {exc.code.value}\n"
        f"Invoice: {exc.invoice_id or 'n/a'}\n"
        f"Credit: {exc.credit_id or 'n/a'}\n"
        f"Amount: Rs {amount_str}\n"
        f"Deterministic explanation: {exc.explanation}\n"
        f"Recommended action: {exc.action}\n\n"
        "Write a 1-2 sentence plain-English narrative for a dashboard, and a "
        "short polite chase message (3-5 sentences) ready to send to the client "
        "or deductor."
    )
    response = call_with_retry(
        client.models.generate_content,
        model=MODEL,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_NarrativeSchema,
        ),
    )
    p = response.parsed
    # re-inject the deterministic amount programmatically rather than trust
    # the model's own restatement of it inside free text
    narrative = p.narrative if amount_str in p.narrative else f"{p.narrative} (Rs {amount_str})"
    return {"narrative": narrative, "chase_message": p.chase_message}
