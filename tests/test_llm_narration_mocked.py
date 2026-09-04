"""
Proves llm.narration is wired correctly - request shape, response
parsing, and gate integration - without a live API key or network
call. The Anthropic client is replaced with a stub whose
messages.parse() returns a fixed, fake response, exactly the shape
the SDK returns from client.messages.parse(..., output_format=Model).
"""

from dataclasses import dataclass
from types import SimpleNamespace

from llm.narration import parse_narration, parse_narration_stub, parse_narration_verified
from llm.verify import VerificationStats


@dataclass
class _FakeParsedOutput:
    counterparty: str | None
    utr: str | None
    rail: str
    reference: str | None


class _FakeMessages:
    def __init__(self, parsed_output):
        self._parsed_output = parsed_output
        self.last_call_kwargs = None

    def parse(self, **kwargs):
        self.last_call_kwargs = kwargs
        return SimpleNamespace(parsed_output=self._parsed_output)


class _FakeClient:
    def __init__(self, parsed_output):
        self.messages = _FakeMessages(parsed_output)


def test_parse_narration_calls_the_expected_model_and_returns_dataclass():
    fake = _FakeClient(_FakeParsedOutput(
        counterparty="Arjun Textiles", utr="452118839021", rail="UPI", reference="inv-ghost-01",
    ))
    result = parse_narration("UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01", client=fake)

    assert result.counterparty == "Arjun Textiles"
    assert result.utr == "452118839021"
    assert result.rail == "UPI"

    kwargs = fake.messages.last_call_kwargs
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["messages"][0]["content"] == "UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01"


def test_parse_narration_verified_discards_a_hallucinated_utr():
    raw = "UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01"
    # the fake model returns a UTR that is NOT present in the raw string
    fake = _FakeClient(_FakeParsedOutput(
        counterparty="Arjun Textiles", utr="000000000000", rail="UPI", reference="inv-ghost-01",
    ))
    stats = VerificationStats()
    result = parse_narration_verified(raw, client=fake, stats=stats)
    assert result is None
    assert stats.total == 1
    assert stats.discarded == 1


def test_parse_narration_verified_passes_a_genuine_extraction():
    raw = "UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01"
    fake = _FakeClient(_FakeParsedOutput(
        counterparty="Arjun Textiles", utr="452118839021", rail="UPI", reference="inv-ghost-01",
    ))
    stats = VerificationStats()
    result = parse_narration_verified(raw, client=fake, stats=stats)
    assert result is not None
    assert stats.discarded == 0


def test_stub_baseline_only_extracts_a_digit_run():
    raw = "UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01"
    result = parse_narration_stub(raw)
    assert result.utr == "452118839021"
    assert result.counterparty is None   # the whole point of the baseline
