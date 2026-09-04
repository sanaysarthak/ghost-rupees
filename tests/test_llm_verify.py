"""
The verification gate is the safety mechanism that makes it acceptable
to let an LLM touch bank narrations at all - these tests run with no
API key and no network, because the gate itself never calls the model.
"""

from llm.verify import ParsedNarration, VerificationStats, gate_narration


def test_valid_utr_present_in_source_passes():
    raw = "UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01"
    parsed = ParsedNarration(counterparty="Arjun Textiles", utr="452118839021",
                              rail="UPI", reference="inv-ghost-01")
    result = gate_narration(raw, parsed)
    assert result is not None
    assert result.utr == "452118839021"


def test_hallucinated_utr_not_in_source_is_discarded():
    raw = "UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01"
    # the model claims a DIFFERENT UTR that never appeared in the source text
    parsed = ParsedNarration(counterparty="Arjun Textiles", utr="999999999999",
                              rail="UPI", reference="inv-ghost-01")
    result = gate_narration(raw, parsed)
    assert result is None


def test_utr_with_formatting_differences_still_matches():
    # UTR present with different casing/punctuation should still pass the
    # hallucination guard - normalisation happens on both sides
    raw = "NEFT/HDFC/452-118-839-021/ArjunTextiles"
    parsed = ParsedNarration(counterparty="Arjun Textiles", utr="452118839021", rail="NEFT", reference=None)
    result = gate_narration(raw, parsed)
    assert result is not None


def test_already_bound_utr_is_discarded():
    raw = "UPI/CR/111122223333/SOMECLIENT/HDFC/x"
    parsed = ParsedNarration(counterparty="Some Client", utr="111122223333", rail="UPI", reference=None)
    result = gate_narration(raw, parsed, is_utr_already_bound=lambda u: True)
    assert result is None


def test_unresolvable_counterparty_is_cleared_not_dropped():
    raw = "UPI/CR/111122223333/UNKNOWNPARTY/HDFC/x"
    parsed = ParsedNarration(counterparty="Unknown Party", utr="111122223333", rail="UPI", reference=None)
    result = gate_narration(raw, parsed, resolve_client=lambda name: False)
    assert result is not None                    # not discarded outright
    assert result.counterparty is None            # but cleared
    assert result.counterparty_status == "UNRESOLVED"
    assert result.utr == "111122223333"           # UTR (verified) is kept


def test_no_utr_claimed_skips_hallucination_guard():
    raw = "some narration with no reference number at all"
    parsed = ParsedNarration(counterparty=None, utr=None, rail="UPI", reference=None)
    result = gate_narration(raw, parsed)
    assert result is not None


def test_verification_stats_discard_rate():
    stats = VerificationStats()
    stats.record(discarded=False)
    stats.record(discarded=False)
    stats.record(discarded=True)
    stats.record(discarded=False)
    assert stats.total == 4
    assert stats.discarded == 1
    assert stats.discard_rate_pct == 25.0
