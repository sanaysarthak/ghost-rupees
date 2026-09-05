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
import time

MODEL = "gemini-3.6-flash"

# gemini-3.6-flash is a very recently released model. Confirmed live on
# 2026-09-05 that it (a) genuinely hits transient 503 "high demand"
# errors under real load, and (b) its free tier caps at a strict 20
# generate_content calls PER DAY PER MODEL (not a short-window rate
# limit - the quota is exhausted for the rest of the day, backoff does
# not help). (b) is the more important fact: Google scopes the free
# quota per model, so a different model gets its own fresh bucket. See
# DECISIONS.md Entry 11.
FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest"]
# Note: the entire gemini-2.5-* line returned 404 "no longer available to
# new users" for the API key this was verified against on 2026-09-05 -
# Google appears to gate older models by project age. Confirmed live
# which models this specific key can actually reach before picking this
# list, rather than guessing plausible-looking names.
MAX_RETRIES = 4
RETRY_BASE_DELAY_SECONDS = 2.0


class LLMNotConfigured(RuntimeError):
    pass


def call_with_retry(fn, *args, **kwargs):
    """
    Calls fn(*args, **kwargs), retrying transient 503s with exponential
    backoff on the SAME model (that's a real "try again in a moment"
    failure), and falling back to the next model in FALLBACK_MODELS on
    429 quota exhaustion (that's a hard daily cap backoff cannot fix,
    but a different model has its own separate quota). Any other
    client error (bad schema, bad API key) raises immediately,
    unretried and un-fallen-back.

    kwargs must include `model=...` for the fallback substitution to
    apply; if absent, only retry (no fallback) is attempted.
    """
    from google.genai import errors

    requested_model = kwargs.get("model")
    models_to_try = [requested_model] if requested_model else [None]
    if requested_model:
        models_to_try += [m for m in FALLBACK_MODELS if m != requested_model]

    last_error: Exception | None = None
    for model_name in models_to_try:
        call_kwargs = dict(kwargs)
        if model_name is not None:
            call_kwargs["model"] = model_name
        for attempt in range(MAX_RETRIES):
            try:
                response = fn(*args, **call_kwargs)
                if model_name is not None and model_name != requested_model:
                    print(f"  (note: {requested_model} was unavailable/quota-exhausted, "
                          f"fell back to {model_name})")
                return response
            except errors.ServerError as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** attempt))
            except errors.ClientError as e:
                if getattr(e, "code", None) == 429:
                    last_error = e
                    break  # daily quota, not a transient blip - move to the next model, don't retry this one
                raise
    raise last_error


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
