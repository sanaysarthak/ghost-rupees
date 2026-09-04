"""
Shared Anthropic client construction, with a clear error message
instead of a raw traceback when no credentials are configured - the
deterministic core (cli.py run) must never require this module to
import cleanly, but anything that DOES call into llm/ should fail
loudly and specifically, not silently or cryptically.
"""

from __future__ import annotations

MODEL = "claude-opus-5"


class LLMNotConfigured(RuntimeError):
    pass


def get_client():
    try:
        import anthropic
    except ImportError as e:
        raise LLMNotConfigured(
            "the `anthropic` package is not installed. Run `pip install anthropic pydantic` "
            "to use the optional LLM layer - the deterministic engine (`cli.py run`) does not need it."
        ) from e

    try:
        client = anthropic.Anthropic()
    except Exception as e:  # noqa: BLE001 - surfaced as a clear, actionable message
        raise LLMNotConfigured(
            "could not construct an Anthropic client - set ANTHROPIC_API_KEY, or run `ant auth login`. "
            f"Underlying error: {e}"
        ) from e
    return client
