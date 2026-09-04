"""
The composition / hypothesis solver.

Does NOT match on gross amount. For each invoice, enumerates every
*lawful and common-error* deduction hypothesis and the net amount it
predicts, so that when a credit arrives, the gap between invoice and
credit can be *identified* (this specific rate, on this specific base)
rather than merely observed. See plan/baaki.md §5 for the worked
example this implements.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.classify import ExceptionCode
from core.models import Client, DeductionKind, Invoice
from core.money import Paisa, apply_bps, gst_inclusive
from core.rules.registry import GST_DOMESTIC_BPS, DeductionBase, RuleSet

# Sibling deduction kinds within the same section family, used to build
# "wrong rate" hypotheses (e.g. 2% technical rate mistakenly applied to
# what should have been a 10% professional-fee invoice).
_194J_FAMILY = (DeductionKind.TDS_PROFESSIONAL_194J, DeductionKind.TDS_TECHNICAL_194J)


@dataclass(frozen=True, slots=True)
class Hypothesis:
    label: str
    predicted_net_paisa: Paisa
    deduction_kind: DeductionKind
    deduction_amount_paisa: Paisa
    lawful: bool
    exception_if_matched: ExceptionCode | None   # None means "fully lawful, no flag"
    explanation: str


def gross_amount(invoice: Invoice) -> Paisa:
    if invoice.gst_applicable:
        return gst_inclusive(invoice.service_amount_paisa, GST_DOMESTIC_BPS)
    return invoice.service_amount_paisa


def hypotheses_for_invoice(invoice: Invoice, ruleset: RuleSet, client: Client,
                            prior_base_paisa_this_fy: Paisa = Paisa(0)) -> list[Hypothesis]:
    """
    prior_base_paisa_this_fy: sum of service_amount_paisa for this client's
    OTHER invoices, earlier in the same financial year. TDS thresholds
    (e.g. 194J's Rs 50,000) apply cumulatively per payee per FY, not per
    invoice - a payer who has already crossed the threshold earlier in
    the year is required to deduct on this invoice even if this
    invoice's own amount is small. Callers (core.match) are responsible
    for computing this from the batch; it defaults to 0, which means
    "treat this as the payee's first invoice this FY" if omitted.
    """
    base = invoice.service_amount_paisa
    gross = gross_amount(invoice)
    kind = invoice.deduction_kind
    hyps: list[Hypothesis] = []

    # H0 - no deduction at all (full gross received)
    hyps.append(Hypothesis(
        label="no_deduction",
        predicted_net_paisa=gross,
        deduction_kind=DeductionKind.NONE,
        deduction_amount_paisa=Paisa(0),
        lawful=True,
        exception_if_matched=None,
        explanation="Full invoiced amount received, no deduction applied.",
    ))

    if kind == DeductionKind.NONE:
        return hyps

    if kind == DeductionKind.PLATFORM_COMMISSION:
        # Not a statutory rate - platform commission is a privately
        # contracted percentage (Client.contracted_commission_bps), not
        # in core.rules.registry at all, and has no Form 26AS concept
        # (core.match._book_matched_invoice must not treat it as TDS).
        if client.contracted_commission_bps is None:
            return hyps
        correct_bps = client.contracted_commission_bps
        ded_correct = apply_bps(base, correct_bps)
        hyps.append(Hypothesis(
            label="lawful_correct", predicted_net_paisa=Paisa(gross - ded_correct),
            deduction_kind=kind, deduction_amount_paisa=ded_correct, lawful=True,
            exception_if_matched=None,
            explanation=f"Platform commission at the contracted {correct_bps/100:.2f}% rate.",
        ))
        over_bps = correct_bps + 300   # a plausible over-charge to model
        ded_over = apply_bps(base, over_bps)
        hyps.append(Hypothesis(
            label="commission_over_contracted_rate", predicted_net_paisa=Paisa(gross - ded_over),
            deduction_kind=kind, deduction_amount_paisa=ded_over, lawful=False,
            exception_if_matched=ExceptionCode.PLATFORM_COMMISSION_VARIANCE,
            explanation=f"Commission charged at {over_bps/100:.2f}%, above the contracted "
                        f"{correct_bps/100:.2f}% rate.",
        ))
        return hyps

    if kind == DeductionKind.GATEWAY_FEE:
        # Also not a statutory rate - the gateway's MDR is set by the
        # merchant's own rate card (Client.contracted_mdr_bps), and GST
        # applies to the fee itself (see plan/baaki.md §7: "Razorpay MDR
        # ... Plus 18% GST on the fee itself") - no Form 26AS concept
        # here either.
        if client.contracted_mdr_bps is None:
            return hyps
        correct_mdr_bps = client.contracted_mdr_bps
        mdr_correct = apply_bps(base, correct_mdr_bps)
        gst_on_mdr_correct = apply_bps(mdr_correct, GST_DOMESTIC_BPS)
        ded_correct = Paisa(mdr_correct + gst_on_mdr_correct)
        hyps.append(Hypothesis(
            label="lawful_correct", predicted_net_paisa=Paisa(gross - ded_correct),
            deduction_kind=kind, deduction_amount_paisa=ded_correct, lawful=True,
            exception_if_matched=None,
            explanation=f"Gateway fee at the contracted {correct_mdr_bps/100:.2f}% MDR "
                        f"plus 18% GST on the fee.",
        ))
        over_mdr_bps = correct_mdr_bps + 50   # a plausible over-charge to model
        mdr_over = apply_bps(base, over_mdr_bps)
        gst_on_mdr_over = apply_bps(mdr_over, GST_DOMESTIC_BPS)
        ded_over = Paisa(mdr_over + gst_on_mdr_over)
        hyps.append(Hypothesis(
            label="gateway_fee_above_rate_card", predicted_net_paisa=Paisa(gross - ded_over),
            deduction_kind=kind, deduction_amount_paisa=ded_over, lawful=False,
            exception_if_matched=ExceptionCode.GATEWAY_FEE_VARIANCE,
            explanation=f"MDR charged at {over_mdr_bps/100:.2f}%, above the contracted "
                        f"{correct_mdr_bps/100:.2f}% rate card (both plus 18% GST on the fee).",
        ))
        return hyps

    rule = ruleset.rule_for(kind, pan_on_file=client.pan_on_file)
    if rule is None:
        return hyps

    cumulative_base = Paisa(int(prior_base_paisa_this_fy) + int(base))
    over_threshold = cumulative_base >= rule.threshold_paisa

    # H1 - the correct, lawful deduction: rate applied to base, excl. GST
    ded_correct = apply_bps(base, rule.rate_bps)
    net_correct = Paisa(gross - ded_correct)
    hyps.append(Hypothesis(
        label="lawful_correct",
        predicted_net_paisa=net_correct,
        deduction_kind=kind,
        deduction_amount_paisa=ded_correct,
        lawful=over_threshold,
        exception_if_matched=(None if over_threshold else ExceptionCode.TDS_BELOW_THRESHOLD),
        explanation=(
            f"{kind.value} at {rule.rate_bps/100:.2f}% on the base amount "
            f"(excl. GST), citing {rule.legacy_citation}."
            + ("" if over_threshold else " NOTE: cumulative FY payments to this payee are below the "
                                          "threshold - deduction should not have applied.")
        ),
    ))

    # H2 - common over-deduction error: rate applied to the GST-inclusive gross
    if invoice.gst_applicable:
        ded_gst_incl = apply_bps(gross, rule.rate_bps)
        net_gst_incl = Paisa(gross - ded_gst_incl)
        hyps.append(Hypothesis(
            label="rate_on_gst_inclusive",
            predicted_net_paisa=net_gst_incl,
            deduction_kind=kind,
            deduction_amount_paisa=ded_gst_incl,
            lawful=False,
            exception_if_matched=ExceptionCode.TDS_ON_GST_INCLUSIVE,
            explanation=(
                f"{kind.value} at {rule.rate_bps/100:.2f}% mistakenly applied to the "
                "GST-inclusive total instead of the base amount - an over-deduction."
            ),
        ))

    # H3 - payer omitted the GST component entirely, but still applied TDS
    # correctly on the base. (worked example: 20,000 base, TDS 2,000 -> 18,000)
    if invoice.gst_applicable:
        net_gst_omitted_with_tds = Paisa(base - ded_correct)
        hyps.append(Hypothesis(
            label="gst_omitted_with_correct_tds",
            predicted_net_paisa=net_gst_omitted_with_tds,
            deduction_kind=kind,
            deduction_amount_paisa=ded_correct,
            lawful=False,
            exception_if_matched=ExceptionCode.GST_OMITTED,
            explanation=(
                "The GST component appears to have been omitted from payment "
                "entirely (payer remitted base only), with TDS correctly applied to the base."
            ),
        ))
        # H3b - GST omitted, no TDS either (a distinct, simpler short-pay case)
        hyps.append(Hypothesis(
            label="gst_omitted_no_deduction",
            predicted_net_paisa=base,
            deduction_kind=DeductionKind.NONE,
            deduction_amount_paisa=Paisa(0),
            lawful=False,
            exception_if_matched=ExceptionCode.GST_OMITTED,
            explanation="The GST component appears to have been omitted from payment; no TDS applied.",
        ))

    # H4 - wrong rate from a sibling deduction kind in the same section family
    # (e.g. 2% technical rate applied to what should be a 10% professional fee)
    if kind in _194J_FAMILY:
        for sibling in _194J_FAMILY:
            if sibling == kind:
                continue
            sibling_rule = ruleset.rule_for(sibling, pan_on_file=client.pan_on_file)
            if sibling_rule is None:
                continue
            ded_sibling = apply_bps(base, sibling_rule.rate_bps)
            net_sibling = Paisa(gross - ded_sibling)
            hyps.append(Hypothesis(
                label=f"wrong_rate_from_{sibling.value}",
                predicted_net_paisa=net_sibling,
                deduction_kind=kind,
                deduction_amount_paisa=ded_sibling,
                lawful=False,
                exception_if_matched=ExceptionCode.TDS_RATE_MISMATCH,
                explanation=(
                    f"Deduction matches the {sibling.value} rate "
                    f"({sibling_rule.rate_bps/100:.2f}%) rather than the {rule.rate_bps/100:.2f}% "
                    f"that should apply to this invoice's {kind.value} classification."
                ),
            ))

    # H5 - PAN-status rate mismatch: the rate that WOULD apply under the
    # opposite PAN-on-file assumption, to catch a 206AA-style error either way.
    opposite_pan = not client.pan_on_file
    alt_rule = ruleset.rule_for(kind, pan_on_file=opposite_pan)
    if alt_rule is not None and alt_rule.rate_bps != rule.rate_bps:
        ded_alt = apply_bps(base, alt_rule.rate_bps)
        net_alt = Paisa(gross - ded_alt)
        hyps.append(Hypothesis(
            label="rate_from_opposite_pan_status",
            predicted_net_paisa=net_alt,
            deduction_kind=kind,
            deduction_amount_paisa=ded_alt,
            lawful=False,
            exception_if_matched=ExceptionCode.TDS_RATE_MISMATCH,
            explanation=(
                f"Deduction matches the {alt_rule.rate_bps/100:.2f}% no-PAN/PAN-on-file "
                "rate rather than the rate that should apply given this client's actual "
                "PAN status on record."
            ),
        ))

    return hyps
