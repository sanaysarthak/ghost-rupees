"""
The held-out eval batch: 14 explicitly-planted, individually-labelled
defects with known ground truth, separate from the golden batch's
random+designed mix (see plan/baaki.md §10 - the track brief's original
12, plus OVER_PAID and GATEWAY_FEE_VARIANCE, added once those exception
codes got real matcher support). Unlike data/generate.py's index-driven
anomaly cycle, every defect here is hand-built so its expected
classification is exact and checkable - this is what eval/defects.py
runs the matcher against and scores.

One of the fourteen is a defect the current engine does NOT yet catch
(FX_SPREAD_UNEXPLAINED) - see the DEFECTS table's `known_gap` field.
Reporting it as an honest miss, with the reason, is the point: "one
cherry-picked match proves nothing" cuts both ways.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.classify import ExceptionCode
from core.models import Batch, Client, Credit, DeductionKind, Form26ASEntry, Invoice, Rail
from core.money import Paisa, apply_bps, rupees_to_paisa
from core.rules.registry import resolve as resolve_ruleset

HOLDOUT_FY_DATE = date(2026, 5, 1)


@dataclass(frozen=True, slots=True)
class PlantedDefect:
    n: int
    description: str
    invoice_id: str | None
    credit_id: str | None
    expected_code: ExceptionCode
    known_gap: str | None = None   # if set, the engine is not expected to catch this yet


DEFECTS: list[PlantedDefect] = []   # populated by build_holdout_batch()


def _client(cid: str, name: str, tan: str | None, pan: bool = True,
            commission_bps: int | None = None, mdr_bps: int | None = None) -> Client:
    return Client(client_id=cid, name=name, pan_on_file=pan, tan=tan,
                  contracted_commission_bps=commission_bps, contracted_mdr_bps=mdr_bps)


def build_holdout_batch() -> tuple[Batch, list[PlantedDefect]]:
    batch = Batch()
    defects: list[PlantedDefect] = []

    c1 = _client("cli_h01", "Harborview Consulting", "HRBV11111A")
    c2 = _client("cli_h02", "Windmere Studio", "WNDM22222B")
    c3 = _client("cli_h03", "Casterbridge Textiles", "CSTB33333C")
    c4 = _client("cli_h04", "Loamfield Agro", "LOAM44444D")
    c5 = _client("cli_h05", "Quillon Media", "QLLN55555E")
    c6 = _client("cli_h06", "Restive Platforms Inc", "RSTV66666F", commission_bps=200)
    c7 = _client("cli_h07", "Millbrook Gateway Services", "MLBK77777G", mdr_bps=180)
    batch.clients.extend([c1, c2, c3, c4, c5, c6, c7])

    ruleset = resolve_ruleset(HOLDOUT_FY_DATE)

    # --- Defect 1: TDS at 10% (professional) where 2% (technical) applies
    prof_rule = ruleset.rule_for(DeductionKind.TDS_PROFESSIONAL_194J)
    base1 = rupees_to_paisa("60000.00")
    batch.invoices.append(Invoice(
        invoice_id="HOLD-01", client_id=c1.client_id, issue_date=date(2026, 5, 4),
        due_date=date(2026, 5, 19), service_amount_paisa=base1, gst_applicable=False,
        deduction_kind=DeductionKind.TDS_TECHNICAL_194J, notes="receipt:HOLD-01",
    ))
    ded1 = apply_bps(base1, prof_rule.rate_bps)   # wrongly deducted at 10%, not 2%
    batch.credits.append(Credit(
        credit_id="HCR-01", value_date=date(2026, 5, 21), amount_paisa=Paisa(base1 - ded1),
        rail=Rail.UPI, raw_narration="UPI/CR/810000000001/HARBORVIEWCONSULTING/HDFC/x", utr="810000000001",
    ))
    batch.form26as.append(Form26ASEntry(
        entry_id="H26-01", deductor_tan=c1.tan, quarter="Q1-FY26-27",
        amount_paisa=ded1, section=prof_rule.legacy_citation,
    ))
    defects.append(PlantedDefect(1, "10% professional rate applied where 2% technical applies",
                                  "HOLD-01", "HCR-01", ExceptionCode.TDS_RATE_MISMATCH))

    # --- Defect 2: TDS deducted although the FY threshold was never crossed
    base2 = rupees_to_paisa("15000.00")   # well below Rs 50,000, first invoice this FY for c2
    ded2 = apply_bps(base2, prof_rule.rate_bps)
    batch.invoices.append(Invoice(
        invoice_id="HOLD-02", client_id=c2.client_id, issue_date=date(2026, 5, 5),
        due_date=date(2026, 5, 20), service_amount_paisa=base2, gst_applicable=False,
        deduction_kind=DeductionKind.TDS_PROFESSIONAL_194J, notes="receipt:HOLD-02",
    ))
    batch.credits.append(Credit(
        credit_id="HCR-02", value_date=date(2026, 5, 22), amount_paisa=Paisa(base2 - ded2),
        rail=Rail.UPI, raw_narration="UPI/CR/810000000002/WINDMERESTUDIO/ICIC/x", utr="810000000002",
    ))
    batch.form26as.append(Form26ASEntry(
        entry_id="H26-02", deductor_tan=c2.tan, quarter="Q1-FY26-27",
        amount_paisa=ded2, section=prof_rule.legacy_citation,
    ))
    defects.append(PlantedDefect(2, "TDS deducted although the FY threshold was never crossed",
                                  "HOLD-02", "HCR-02", ExceptionCode.TDS_BELOW_THRESHOLD))

    # --- Defect 3: TDS computed on the GST-inclusive amount (over-deduction)
    base3 = rupees_to_paisa("70000.00")
    from core.compose import gross_amount as _gross
    inv3 = Invoice(
        invoice_id="HOLD-03", client_id=c3.client_id, issue_date=date(2026, 5, 6),
        due_date=date(2026, 5, 21), service_amount_paisa=base3, gst_applicable=True,
        deduction_kind=DeductionKind.TDS_PROFESSIONAL_194J, notes="receipt:HOLD-03",
    )
    batch.invoices.append(inv3)
    gross3 = _gross(inv3)
    ded3 = apply_bps(gross3, prof_rule.rate_bps)   # wrongly applied to GST-inclusive total
    batch.credits.append(Credit(
        credit_id="HCR-03", value_date=date(2026, 5, 23), amount_paisa=Paisa(gross3 - ded3),
        rail=Rail.NEFT, raw_narration="NEFT/HDFC/810000000003/CASTERBRIDGETEXTILES", utr="810000000003",
    ))
    batch.form26as.append(Form26ASEntry(
        entry_id="H26-03", deductor_tan=c3.tan, quarter="Q1-FY26-27",
        amount_paisa=ded3, section=prof_rule.legacy_citation,
    ))
    defects.append(PlantedDefect(3, "TDS computed on the GST-inclusive amount",
                                  "HOLD-03", "HCR-03", ExceptionCode.TDS_ON_GST_INCLUSIVE))

    # --- Defects 4-5: TDS deducted but absent from Form 26AS, two different clients
    for i, (client, hold_id, cr_id, ref) in enumerate([
        (c1, "HOLD-04", "HCR-04", "810000000004"), (c4, "HOLD-05", "HCR-05", "810000000005"),
    ], start=4):
        base = rupees_to_paisa("55000.00")
        ded = apply_bps(base, prof_rule.rate_bps)
        batch.invoices.append(Invoice(
            invoice_id=hold_id, client_id=client.client_id, issue_date=date(2026, 5, 7 + i),
            due_date=date(2026, 5, 22 + i), service_amount_paisa=base, gst_applicable=False,
            deduction_kind=DeductionKind.TDS_PROFESSIONAL_194J, notes=f"receipt:{hold_id}",
        ))
        batch.credits.append(Credit(
            credit_id=cr_id, value_date=date(2026, 5, 24 + i), amount_paisa=Paisa(base - ded),
            rail=Rail.UPI, raw_narration=f"UPI/CR/{ref}/{client.name.upper().replace(' ', '')}/HDFC/x", utr=ref,
        ))
        # deliberately NO Form26ASEntry
        defects.append(PlantedDefect(i, f"TDS deducted but absent from Form 26AS ({client.name})",
                                      hold_id, cr_id, ExceptionCode.TDS_NOT_IN_26AS))

    # --- Defect 6: short payment, no lawful basis
    base6 = rupees_to_paisa("30000.00")
    batch.invoices.append(Invoice(
        invoice_id="HOLD-06", client_id=c5.client_id, issue_date=date(2026, 5, 10),
        due_date=date(2026, 5, 25), service_amount_paisa=base6, gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:HOLD-06",
    ))
    batch.credits.append(Credit(
        credit_id="HCR-06", value_date=date(2026, 5, 27),
        amount_paisa=Paisa(base6 - rupees_to_paisa("4000.00")), rail=Rail.UPI,
        raw_narration="UPI/CR/810000000006/QUILLONMEDIA/ICIC/partial", utr="810000000006",
    ))
    defects.append(PlantedDefect(6, "Short payment with no lawful deduction basis",
                                  "HOLD-06", "HCR-06", ExceptionCode.SHORT_PAID))

    # --- Defect 7: duplicate credit, same UTR
    base7 = rupees_to_paisa("22000.00")
    batch.invoices.append(Invoice(
        invoice_id="HOLD-07", client_id=c2.client_id, issue_date=date(2026, 5, 11),
        due_date=date(2026, 5, 26), service_amount_paisa=base7, gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:HOLD-07",
    ))
    batch.credits.append(Credit(
        credit_id="HCR-07A", value_date=date(2026, 5, 28), amount_paisa=base7,
        rail=Rail.UPI, raw_narration="UPI/CR/810000000007/WINDMERESTUDIO/ICIC/x", utr="810000000007",
    ))
    batch.credits.append(Credit(
        credit_id="HCR-07B", value_date=date(2026, 5, 28), amount_paisa=base7,
        rail=Rail.UPI, raw_narration="UPI/CR/810000000007/WINDMERESTUDIO/ICIC/dup", utr="810000000007",
    ))
    defects.append(PlantedDefect(7, "Duplicate credit, same UTR seen twice",
                                  None, "HCR-07B", ExceptionCode.DUPLICATE_CREDIT))

    # --- Defect 8: one invoice settled across three credits
    base8 = rupees_to_paisa("90000.00")
    batch.invoices.append(Invoice(
        invoice_id="HOLD-08", client_id=c3.client_id, issue_date=date(2026, 5, 12),
        due_date=date(2026, 5, 27), service_amount_paisa=base8, gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:HOLD-08",
    ))
    for j, amt in enumerate(["30000.00", "30000.00", "30000.00"]):
        batch.credits.append(Credit(
            credit_id=f"HCR-08{chr(65+j)}", value_date=date(2026, 5, 29 + j),
            amount_paisa=rupees_to_paisa(amt), rail=Rail.UPI,
            raw_narration=f"UPI/CR/81000000080{j}/CASTERBRIDGETEXTILES/HDFC/part{j+1}", utr=f"81000000080{j}",
        ))
    defects.append(PlantedDefect(8, "One invoice settled across three separate credits",
                                  "HOLD-08", "HCR-08A,HCR-08B,HCR-08C", ExceptionCode.SPLIT_PAYMENT))

    # --- Defect 9: one credit covering two invoices
    base9a, base9b = rupees_to_paisa("18000.00"), rupees_to_paisa("22000.00")
    batch.invoices.append(Invoice(
        invoice_id="HOLD-09A", client_id=c4.client_id, issue_date=date(2026, 5, 13),
        due_date=date(2026, 5, 28), service_amount_paisa=base9a, gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:HOLD-09A",
    ))
    batch.invoices.append(Invoice(
        invoice_id="HOLD-09B", client_id=c4.client_id, issue_date=date(2026, 5, 14),
        due_date=date(2026, 5, 29), service_amount_paisa=base9b, gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:HOLD-09B",
    ))
    batch.credits.append(Credit(
        credit_id="HCR-09", value_date=date(2026, 5, 30), amount_paisa=Paisa(base9a + base9b),
        rail=Rail.UPI, raw_narration="UPI/CR/810000000009/LOAMFIELDAGRO/ICIC/combined", utr="810000000009",
    ))
    defects.append(PlantedDefect(9, "One credit covering two invoices (merged payment)",
                                  "HOLD-09A,HOLD-09B", "HCR-09", ExceptionCode.MERGED_PAYMENT))

    # --- Defect 10: platform commission at 5% where the contract says 2%
    base10 = rupees_to_paisa("40000.00")
    inv10 = Invoice(
        invoice_id="HOLD-10", client_id=c6.client_id, issue_date=date(2026, 5, 15),
        due_date=date(2026, 5, 30), service_amount_paisa=base10, gst_applicable=False,
        deduction_kind=DeductionKind.PLATFORM_COMMISSION, notes="receipt:HOLD-10",
    )
    batch.invoices.append(inv10)
    ded10 = apply_bps(base10, 500)   # charged at 5%, contracted rate is 2%
    batch.credits.append(Credit(
        credit_id="HCR-10", value_date=date(2026, 5, 31), amount_paisa=Paisa(base10 - ded10),
        rail=Rail.UPI, raw_narration="UPI/CR/810000000010/RESTIVEPLATFORMSINC/HDFC/x", utr="810000000010",
    ))
    defects.append(PlantedDefect(10, "Platform commission charged at 5% where the contract says 2%",
                                  "HOLD-10", "HCR-10", ExceptionCode.PLATFORM_COMMISSION_VARIANCE))

    # --- Defect 11: credit with no matching invoice - untracked income
    batch.credits.append(Credit(
        credit_id="HCR-11", value_date=date(2026, 6, 2), amount_paisa=rupees_to_paisa("5000.00"),
        rail=Rail.UPI, raw_narration="UPI/CR/810000000011/UNKNOWNSENDER/ICIC/x", utr="810000000011",
    ))
    defects.append(PlantedDefect(11, "Credit with no matching invoice - untracked income",
                                  None, "HCR-11", ExceptionCode.UNMATCHED_CREDIT))

    # --- Defect 12: foreign wire with an unexplained FX shortfall (KNOWN GAP)
    base12 = rupees_to_paisa("100000.00")
    batch.invoices.append(Invoice(
        invoice_id="HOLD-12", client_id=c5.client_id, issue_date=date(2026, 5, 16),
        due_date=date(2026, 5, 31), service_amount_paisa=base12, gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:HOLD-12",
    ))
    batch.credits.append(Credit(
        credit_id="HCR-12", value_date=date(2026, 6, 3),
        amount_paisa=Paisa(base12 - rupees_to_paisa("3200.00")), rail=Rail.WIRE,
        raw_narration="WIRE INW SWIFT/QUILLONMEDIA/FX-SPREAD-ADJ", utr=None,
    ))
    defects.append(PlantedDefect(
        12, "Foreign wire with an unexplained FX shortfall", "HOLD-12", "HCR-12",
        ExceptionCode.FX_SPREAD_UNEXPLAINED,
        known_gap="No FX-spread hypothesis exists yet in core.compose - a wire shortfall this "
                  "size (>Rs 500) currently falls outside the SHORT_PAY_MIN_FRACTION floor logic "
                  "path too, so this is an honest, expected miss until FX handling is built.",
    ))

    # --- Defect 13: a customer pays more than invoiced - a duplicate
    # transfer folded into the same credit, no lawful basis for the excess
    base13 = rupees_to_paisa("45000.00")
    batch.invoices.append(Invoice(
        invoice_id="HOLD-13", client_id=c2.client_id, issue_date=date(2026, 5, 17),
        due_date=date(2026, 6, 1), service_amount_paisa=base13, gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:HOLD-13",
    ))
    batch.credits.append(Credit(
        credit_id="HCR-13", value_date=date(2026, 6, 5),
        amount_paisa=Paisa(base13 + rupees_to_paisa("6000.00")), rail=Rail.UPI,
        raw_narration="UPI/CR/810000000013/WINDMERESTUDIO/ICIC/overpay", utr="810000000013",
    ))
    defects.append(PlantedDefect(13, "Customer paid Rs 6,000 more than invoiced, no lawful basis",
                                  "HOLD-13", "HCR-13", ExceptionCode.OVER_PAID))

    # --- Defect 14: gateway (MDR) fee charged above the contracted rate card
    from core.compose import gross_amount as _gross14
    base14 = rupees_to_paisa("25000.00")
    inv14 = Invoice(
        invoice_id="HOLD-14", client_id=c7.client_id, issue_date=date(2026, 5, 18),
        due_date=date(2026, 6, 2), service_amount_paisa=base14, gst_applicable=False,
        deduction_kind=DeductionKind.GATEWAY_FEE, notes="receipt:HOLD-14",
    )
    batch.invoices.append(inv14)
    over_mdr_bps = c7.contracted_mdr_bps + 50   # matches compose.py's over-charge model exactly
    mdr_over = apply_bps(base14, over_mdr_bps)
    gst_on_mdr_over = apply_bps(mdr_over, 1800)
    ded14 = Paisa(mdr_over + gst_on_mdr_over)
    batch.credits.append(Credit(
        credit_id="HCR-14", value_date=date(2026, 6, 6),
        amount_paisa=Paisa(_gross14(inv14) - ded14), rail=Rail.UPI,
        raw_narration="UPI/CR/810000000014/MILLBROOKGATEWAYSERVICES/HDFC/x", utr="810000000014",
    ))
    defects.append(PlantedDefect(14, "Gateway MDR charged above the contracted rate card",
                                  "HOLD-14", "HCR-14", ExceptionCode.GATEWAY_FEE_VARIANCE))

    return batch, defects
