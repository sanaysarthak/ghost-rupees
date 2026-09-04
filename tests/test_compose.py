"""Validates the exact worked example from plan/baaki.md §5:

Invoice: Rs 20,000 professional services, GST shown separately -> gross Rs 23,600.
H1 lawful_correct               -> 21,600
H2 rate_on_gst_inclusive        -> 21,240
H3 gst_omitted_with_correct_tds -> 18,000
H0 no_deduction                 -> 23,600
"""

from datetime import date

from core.compose import gross_amount, hypotheses_for_invoice
from core.models import Client, DeductionKind, Invoice
from core.money import rupees_to_paisa
from core.rules.registry import resolve as resolve_ruleset


def _client(pan_on_file=True):
    return Client(client_id="cli_test", name="Arjun Textiles Pvt Ltd", pan_on_file=pan_on_file, tan="DELA12345B")


def _invoice():
    return Invoice(
        invoice_id="INV-014", client_id="cli_test",
        issue_date=date(2026, 6, 4), due_date=date(2026, 6, 19),
        service_amount_paisa=rupees_to_paisa("20000.00"), gst_applicable=True,
        deduction_kind=DeductionKind.TDS_PROFESSIONAL_194J,
    )


def test_gross_amount_includes_gst():
    inv = _invoice()
    assert gross_amount(inv) == rupees_to_paisa("23600.00")


def test_worked_example_hypotheses():
    inv = _invoice()
    client = _client()
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}

    assert hyps["no_deduction"].predicted_net_paisa == rupees_to_paisa("23600.00")
    assert hyps["lawful_correct"].predicted_net_paisa == rupees_to_paisa("21600.00")
    assert hyps["rate_on_gst_inclusive"].predicted_net_paisa == rupees_to_paisa("21240.00")
    assert hyps["gst_omitted_with_correct_tds"].predicted_net_paisa == rupees_to_paisa("18000.00")


def test_lawful_correct_is_marked_lawful_when_fy_threshold_already_crossed():
    """
    The 194J threshold (Rs 50,000) applies cumulatively per payee per FY,
    not per invoice - see core.compose's docstring on
    prior_base_paisa_this_fy. This invoice's own base (Rs 20,000) is below
    the threshold on its own; it only becomes a lawful deduction once
    combined with what this payee already invoiced the same client earlier
    in the FY. Rs 40,000 prior + Rs 20,000 this invoice = Rs 60,000, over
    the Rs 50,000 threshold.
    """
    inv = _invoice()
    client = _client()
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(
        inv, ruleset, client, prior_base_paisa_this_fy=rupees_to_paisa("40000.00"),
    )}
    assert hyps["lawful_correct"].lawful is True
    assert hyps["rate_on_gst_inclusive"].lawful is False
    assert hyps["gst_omitted_with_correct_tds"].lawful is False


def test_lawful_correct_is_unlawful_when_threshold_not_yet_crossed():
    """Same invoice, but as this payee's first invoice this FY (no prior
    cumulative amount) - Rs 20,000 alone is below the Rs 50,000 threshold,
    so a deduction here would NOT be lawful."""
    inv = _invoice()
    client = _client()
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}
    assert hyps["lawful_correct"].lawful is False
    assert hyps["lawful_correct"].exception_if_matched is not None


def test_below_threshold_flagged():
    inv = Invoice(
        invoice_id="INV-SMALL", client_id="cli_test",
        issue_date=date(2026, 6, 4), due_date=date(2026, 6, 19),
        service_amount_paisa=rupees_to_paisa("10000.00"), gst_applicable=False,
        deduction_kind=DeductionKind.TDS_PROFESSIONAL_194J,
    )
    client = _client()
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}
    # below the Rs 50,000 threshold - a deduction here would be unlawful
    assert hyps["lawful_correct"].lawful is False
    assert hyps["lawful_correct"].exception_if_matched is not None


def test_rate_mismatch_hypothesis_present_for_194j_family():
    inv = _invoice()
    client = _client()
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}
    assert "wrong_rate_from_TDS_TECHNICAL_194J" in hyps
    assert hyps["wrong_rate_from_TDS_TECHNICAL_194J"].lawful is False


def test_platform_commission_uses_contracted_rate_not_statutory_table():
    from core.classify import ExceptionCode

    client = Client(client_id="cli_platform", name="Some Platform Client",
                     pan_on_file=True, tan=None, contracted_commission_bps=200)  # 2%
    inv = Invoice(
        invoice_id="INV-PLAT-01", client_id="cli_platform",
        issue_date=date(2026, 6, 4), due_date=date(2026, 6, 19),
        service_amount_paisa=rupees_to_paisa("10000.00"), gst_applicable=False,
        deduction_kind=DeductionKind.PLATFORM_COMMISSION,
    )
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}

    assert hyps["lawful_correct"].deduction_amount_paisa == rupees_to_paisa("200.00")  # 2% of 10,000
    assert hyps["lawful_correct"].lawful is True
    assert hyps["commission_over_contracted_rate"].exception_if_matched == ExceptionCode.PLATFORM_COMMISSION_VARIANCE
    assert hyps["commission_over_contracted_rate"].deduction_amount_paisa == rupees_to_paisa("500.00")  # 5%


def test_platform_commission_with_no_contracted_rate_yields_no_deduction_hypothesis():
    client = Client(client_id="cli_noplat", name="No Contract Client",
                     pan_on_file=True, tan=None, contracted_commission_bps=None)
    inv = Invoice(
        invoice_id="INV-PLAT-02", client_id="cli_noplat",
        issue_date=date(2026, 6, 4), due_date=date(2026, 6, 19),
        service_amount_paisa=rupees_to_paisa("10000.00"), gst_applicable=False,
        deduction_kind=DeductionKind.PLATFORM_COMMISSION,
    )
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}
    assert list(hyps.keys()) == ["no_deduction"]


def test_gateway_fee_uses_contracted_mdr_plus_gst_on_the_fee():
    """
    Rs 20,000 base, MDR contracted at 2%: fee = 400.00, GST on the fee
    (18%) = 72.00, total deduction = 472.00. See plan/baaki.md §7:
    "Razorpay MDR ... Plus 18% GST on the fee itself."
    """
    from core.classify import ExceptionCode

    client = Client(client_id="cli_gateway", name="Some Gateway Client",
                     pan_on_file=True, tan=None, contracted_mdr_bps=200)  # 2%
    inv = Invoice(
        invoice_id="INV-GATE-01", client_id="cli_gateway",
        issue_date=date(2026, 6, 4), due_date=date(2026, 6, 19),
        service_amount_paisa=rupees_to_paisa("20000.00"), gst_applicable=False,
        deduction_kind=DeductionKind.GATEWAY_FEE,
    )
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}

    assert hyps["lawful_correct"].deduction_amount_paisa == rupees_to_paisa("472.00")
    assert hyps["lawful_correct"].lawful is True
    assert hyps["gateway_fee_above_rate_card"].exception_if_matched == ExceptionCode.GATEWAY_FEE_VARIANCE
    # 2.5% MDR (200 + 50 bps) = 500.00, GST on fee (18%) = 90.00, total 590.00
    assert hyps["gateway_fee_above_rate_card"].deduction_amount_paisa == rupees_to_paisa("590.00")


def test_gateway_fee_with_no_contracted_rate_yields_no_deduction_hypothesis():
    client = Client(client_id="cli_nogate", name="No Rate Card Client",
                     pan_on_file=True, tan=None, contracted_mdr_bps=None)
    inv = Invoice(
        invoice_id="INV-GATE-02", client_id="cli_nogate",
        issue_date=date(2026, 6, 4), due_date=date(2026, 6, 19),
        service_amount_paisa=rupees_to_paisa("20000.00"), gst_applicable=False,
        deduction_kind=DeductionKind.GATEWAY_FEE,
    )
    ruleset = resolve_ruleset(inv.issue_date)
    hyps = {h.label: h for h in hypotheses_for_invoice(inv, ruleset, client)}
    assert list(hyps.keys()) == ["no_deduction"]
