"""
Shared Gemini client construction, with a clear error message instead
of a raw traceback when no credentials are configured - the
deterministic core (cli.py run) must never require this module to
import cleanly, but anything that DOES call into llm/ should fail
loudly and specifically, not silently or cryptically.

Originally built against Anthropic's Claude API; switched to Google's
Gemini API (google-genai SDK) - see DECISIONS.md Entry 10. The trust
boundary and verification-gate design (llm/verify.py) are provider-
agnostic and unchanged by this switch.
"""

from __future__ import annotations

import os

MODEL = "gemini-2.5-flash"


class LLMNotConfigured(RuntimeError):
    pass


def get_client():
    try:
        from google import genai
    except ImportError as e:
        raise LLMNotConfigured(
            "the `google-genai` package is not installed. Run `pip install google-genai pydantic` "
            "to use the optional LLM layer - the deterministic engine (`cli.py run`) does not need it."
        ) from e

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise LLMNotConfigured(
            "no Gemini API key found - set the GEMINI_API_KEY environment variable. "
            "Get a free key at https://aistudio.google.com/apikey"
        )

    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:  # noqa: BLE001 - surfaced as a clear, actionable message
        raise LLMNotConfigured(f"could not construct a Gemini client. Underlying error: {e}") from e
    return client
