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
    batch = generate_batch(seed=42, n_random=40)
    ledger = run_matcher(batch)
    # must not raise - every invoice's buckets sum exactly to its gross amount
    ledger.assert_conserves(batch.invoices, gross_amount)


def test_rupees_accounted_for_is_100_percent_by_construction():
    batch = generate_batch(seed=42, n_random=40)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)
    assert ledger.rupees_accounted_pct(batch.invoices, gross_amount) == 100.0


def test_auto_match_rate_is_meaningfully_high():
    batch = generate_batch(seed=42, n_random=40)
    ledger = run_matcher(batch)
    rate = ledger.auto_match_rate_pct(batch.invoices)
    assert rate >= 50.0, f"auto-match rate {rate}% is too low to be a credible submission"


def test_the_ghost_invoice_is_found_and_named():
    """The canonical worked example: TDS deducted, correct rate, but the
    deductor never deposited it - absent from Form 26AS."""
    batch = generate_batch(seed=42, n_random=40)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)

    ghost_exceptions = [
        e for e in ledger.invoice_exceptions
        if e.invoice_id == "INV-GHOST-01" and e.code == ExceptionCode.TDS_NOT_IN_26AS
    ]
    assert len(ghost_exceptions) == 1
    assert ghost_exceptions[0].amount_paisa == rupees_to_paisa("2000.00")


def test_split_payment_invoice_is_fully_matched():
    batch = generate_batch(seed=42, n_random=40)
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
    batch = generate_batch(seed=42, n_random=40)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)
    codes = {e.code for e in ledger.credit_exceptions}
    # the generator deliberately injects both personal-transfer credits and
    # duplicate-UTR credits, so both codes should be observable
    assert ExceptionCode.UNMATCHED_CREDIT in codes
    assert ExceptionCode.DUPLICATE_CREDIT in codes


def test_cross_client_tie_is_wrong_without_a_narration_hint():
    """
    Documents a real, dangerous defect class: two different clients
    invoice the identical amount, paid by generic UPI credits with no
    Razorpay identifiers and no UTR either invoice references. Amount+
    date matching alone (stage 3) cannot tell them apart, and without
    narration-derived counterparty names to break the tie, the matcher
    picks whichever credit it encountered first - which is wrong here by
    deliberate construction. Conservation still holds (the ledger
    balances either way), which is exactly why this bug is dangerous: it
    is invisible to Gate 1 and to the auto-match rate.
    """
    batch = generate_batch(seed=42, n_random=40)
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)

    line_a = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-A" and l.proof)
    line_b = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-B" and l.proof)
    # deliberately asserting the WRONG outcome - this is what "no
    # disambiguation available" actually produces today
    assert line_a.proof.credit_id == "CR-TIE-B"
    assert line_b.proof.credit_id == "CR-TIE-A"


def test_narration_hint_resolves_the_cross_client_tie_correctly():
    """The same scenario, given a hint shaped exactly like what
    llm.narration.parse_narration_verified produces (credit_id ->
    (counterparty, utr)) - the tie resolves to the correct credit for
    both invoices."""
    batch = generate_batch(seed=42, n_random=40)
    hint = {
        "CR-TIE-A": ("BluePeak Consulting", "700011122233"),
        "CR-TIE-B": ("Fernhill Media", "700011122244"),
    }
    ledger = run_matcher(batch, narration_hint=hint)
    ledger.assert_conserves(batch.invoices, gross_amount)

    line_a = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-A" and l.proof)
    line_b = next(l for l in ledger.lines if l.invoice_id == "INV-TIE-B" and l.proof)
    assert line_a.proof.credit_id == "CR-TIE-A"
    assert line_b.proof.credit_id == "CR-TIE-B"


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
