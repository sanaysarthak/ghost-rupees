"""
Gate 1: the end-to-end run over the golden batch must reconcile to a
zero residual. This is the single most important test in the project -
see plan/baaki.md §13.
"""

from core.classify import ExceptionCode
from core.compose import gross_amount
from core.ledger import LedgerImbalanceError
from core.match import run_matcher
from core.money import rupees_to_paisa
from data.generate import generate_batch


def test_gate1_golden_batch_reconciles_to_zero_residual():
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    # must not raise - every invoice's buckets sum exactly to its gross amount
    ledger.assert_conserves(batch.invoices, gross_amount)


def test_rupees_accounted_for_is_100_percent_by_construction():
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)
    assert ledger.rupees_accounted_pct(batch.invoices, gross_amount) == 100.0


def test_batch_has_at_least_50_invoices():
    batch = generate_batch(seed=42, n_random=60)
    assert len(batch.invoices) >= 50, "track brief requires a 50+ record batch"


def test_auto_match_rate_is_meaningfully_high():
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    rate = ledger.auto_match_rate_pct(batch.invoices)
    assert rate >= 50.0, f"auto-match rate {rate}% is too low to be a credible submission"


def test_the_ghost_invoice_is_found_and_named():
    """The canonical worked example: TDS deducted, correct rate, but the
    deductor never deposited it - absent from Form 26AS."""
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)

    ghost_exceptions = [
        e for e in ledger.invoice_exceptions
        if e.invoice_id == "INV-GHOST-01" and e.code == ExceptionCode.TDS_NOT_IN_26AS
    ]
    assert len(ghost_exceptions) == 1
    assert ghost_exceptions[0].amount_paisa == rupees_to_paisa("2000.00")


def test_split_payment_invoice_is_fully_matched():
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)

    split_exceptions = [e for e in ledger.invoice_exceptions if e.invoice_id == "INV-SPLIT-01"]
    codes = {e.code for e in split_exceptions}
    assert ExceptionCode.SPLIT_PAYMENT in codes
    # and it must NOT also be sitting in SHORT
    unmatched_codes = {e.code for e in ledger.invoice_exceptions if e.invoice_id == "INV-SPLIT-01"
                        and e.code == ExceptionCode.UNMATCHED_INVOICE}
    assert not unmatched_codes


def test_a_credit_exception_exists_for_unmatched_money_in():
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)
    codes = {e.code for e in ledger.credit_exceptions}
    # the generator deliberately injects both personal-transfer credits and
    # duplicate-UTR credits, so both codes should be observable
    assert ExceptionCode.UNMATCHED_CREDIT in codes
    assert ExceptionCode.DUPLICATE_CREDIT in codes


def test_cross_client_tie_resolves_deterministically_via_substring_match():
    """
    Two different clients invoice the identical amount, paid by generic
    UPI credits with no Razorpay identifiers and no UTR either invoice
    references, so amount+date alone (stage 3's core comparison) cannot
    tell them apart. Here the narration spells the counterparty name out
    in full ("BLUEPEAKCONSULTING"), so core.match's tier-1 free substring
    check resolves the tie correctly with NO model call - this is a
    regression test for that deterministic tier, not the LLM ablation
    (see the garbled-name pair, INV-TIE-C/D, and
    test_narration_hint_resolves_a_tie_the_substring_check_cannot for that).
    """
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)

    line_a = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-A" and l.proof)
    line_b = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-B" and l.proof)
    assert line_a.proof.credit_id == "CR-TIE-A"
    assert line_b.proof.credit_id == "CR-TIE-B"


def test_garbled_name_tie_is_declined_not_guessed_without_llm_help():
    """
    INV-TIE-C/D: the genuine ablation scenario. The narration abbreviates
    the counterparty name ("BLUPEAK CNSLTNG", "FRNHL MEDIA") the way real
    bank narrations often do - close enough for a human or an LLM to
    recognise, but NOT a substring of the full client name, so tier 1
    cannot resolve it. Both narrations DO carry other readable text, so
    the matcher can tell that neither tied candidate actually names this
    client - it declines to guess (an earlier version of this matcher
    picked arbitrarily and got it silently wrong instead). Both invoices
    end up honestly unresolved (UNMATCHED_INVOICE) rather than
    confidently wrong.
    """
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)

    line_c = next((l for l in ledger.lines if l.invoice_id == "INV-TIE-C" and l.proof), None)
    line_d = next((l for l in ledger.lines if l.invoice_id == "INV-TIE-D" and l.proof), None)
    assert line_c is None and line_d is None   # declined, not silently matched to the wrong credit

    codes_c = {e.code for e in ledger.invoice_exceptions if e.invoice_id == "INV-TIE-C"}
    codes_d = {e.code for e in ledger.invoice_exceptions if e.invoice_id == "INV-TIE-D"}
    assert ExceptionCode.UNMATCHED_INVOICE in codes_c
    assert ExceptionCode.UNMATCHED_INVOICE in codes_d


def test_narration_hint_resolves_a_tie_the_substring_check_cannot():
    """The same garbled-name scenario, given a hint shaped exactly like
    what llm.narration.parse_narration_verified produces (credit_id ->
    (counterparty, utr)) - simulating an LLM correctly expanding the
    abbreviation. The tie resolves to the correct credit for both
    invoices where the free deterministic check alone could not."""
    batch = generate_batch(seed=42, n_random=60)
    hint = {
        "CR-TIE-C": ("BluePeak Consulting", "700099911122"),
        "CR-TIE-D": ("Fernhill Media", "700099911133"),
    }
    ledger = run_matcher(batch, narration_hint=hint)
    ledger.assert_conserves(batch.invoices, gross_amount)

    line_c = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-C" and l.proof)
    line_d = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-D" and l.proof)
    assert line_c.proof.credit_id == "CR-TIE-C"
    assert line_d.proof.credit_id == "CR-TIE-D"

    line_a = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-A" and l.proof)
    line_b = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-B" and l.proof)
    assert line_a.proof.credit_id == "CR-TIE-A"
    assert line_b.proof.credit_id == "CR-TIE-B"


def test_short_paid_is_recognised_not_swallowed_as_unmatched():
    """
    A partial payment with no lawful deduction basis must be recognised
    as SHORT_PAID (RECEIVED = what arrived, SHORT = the shortfall), not
    silently lumped into UNMATCHED_INVOICE (which would put the ENTIRE
    gross into SHORT and lose the fact that most of the money did
    arrive). This was a real gap that has since been fixed.
    """
    batch = generate_batch(seed=42, n_random=60)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)
    short_paid = [e for e in ledger.invoice_exceptions if e.code == ExceptionCode.SHORT_PAID]
    assert len(short_paid) >= 1
    # for at least one SHORT_PAID invoice, RECEIVED must be nonzero -
    # proving it's a partial match, not a full write-off
    for e in short_paid:
        received_lines = [
            l for l in ledger.lines
            if l.invoice_id == e.invoice_id and l.bucket.value == "RECEIVED"
        ]
        assert received_lines and int(received_lines[0].amount_paisa) > 0


def test_conservation_actually_catches_a_broken_engine():
    """Negative control: prove assert_conserves is not a tautology by
    feeding it a deliberately unbalanced ledger."""
    from core.ledger import Bucket, Ledger, LedgerLine
    from core.models import DeductionKind, Invoice
    from datetime import date

    inv = Invoice(
        invoice_id="INV-BROKEN", client_id="cli_x", issue_date=date(2026, 1, 1),
        due_date=date(2026, 1, 15), service_amount_paisa=rupees_to_paisa("1000.00"),
        gst_applicable=False, deduction_kind=DeductionKind.NONE,
    )
    ledger = Ledger()
    # deliberately book less than the gross amount
    ledger.add(LedgerLine(invoice_id="INV-BROKEN", bucket=Bucket.RECEIVED,
                           amount_paisa=rupees_to_paisa("900.00")))
    try:
        ledger.assert_conserves([inv], gross_amount)
        assert False, "expected LedgerImbalanceError"
    except LedgerImbalanceError as e:
        assert "INV-BROKEN" in str(e)
