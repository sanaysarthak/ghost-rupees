"""The typed exception taxonomy. See plan/baaki.md §6 for the source table."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.money import Paisa


class ExceptionCode(str, Enum):
    TDS_NOT_IN_26AS = "TDS_NOT_IN_26AS"
    TDS_RATE_MISMATCH = "TDS_RATE_MISMATCH"
    TDS_ON_GST_INCLUSIVE = "TDS_ON_GST_INCLUSIVE"
    TDS_BELOW_THRESHOLD = "TDS_BELOW_THRESHOLD"
    GST_OMITTED = "GST_OMITTED"
    SHORT_PAID = "SHORT_PAID"
    OVER_PAID = "OVER_PAID"
    PLATFORM_COMMISSION_VARIANCE = "PLATFORM_COMMISSION_VARIANCE"
    GATEWAY_FEE_VARIANCE = "GATEWAY_FEE_VARIANCE"
    FX_SPREAD_UNEXPLAINED = "FX_SPREAD_UNEXPLAINED"
    UNMATCHED_CREDIT = "UNMATCHED_CREDIT"
    UNMATCHED_INVOICE = "UNMATCHED_INVOICE"
    DUPLICATE_CREDIT = "DUPLICATE_CREDIT"
    SPLIT_PAYMENT = "SPLIT_PAYMENT"
    MERGED_PAYMENT = "MERGED_PAYMENT"


# Human-facing action text per code - used by the report and by the
# (optional) LLM narrative job as the deterministic seed it must not
# contradict.
ACTION_FOR_CODE: dict[ExceptionCode, str] = {
    ExceptionCode.TDS_NOT_IN_26AS: "Chase deductor for a revised TDS return and a corrected certificate.",
    ExceptionCode.TDS_RATE_MISMATCH: "Request a correction; recover the excess deduction.",
    ExceptionCode.TDS_ON_GST_INCLUSIVE: "Request a correction; TDS should apply to the base amount only.",
    ExceptionCode.TDS_BELOW_THRESHOLD: "Request a refund of the deduction; the FY threshold was not crossed.",
    ExceptionCode.GST_OMITTED: "Confirm with the client whether the GST component will be settled separately.",
    ExceptionCode.SHORT_PAID: "Chase the client, citing the invoice reference and shortfall amount.",
    ExceptionCode.OVER_PAID: "Confirm the extra amount before spending it - may be a duplicate or an advance.",
    ExceptionCode.PLATFORM_COMMISSION_VARIANCE: "Raise the commission discrepancy with the platform.",
    ExceptionCode.GATEWAY_FEE_VARIANCE: "Raise the fee discrepancy with the payment gateway.",
    ExceptionCode.FX_SPREAD_UNEXPLAINED: "Request the remittance advice from the bank.",
    ExceptionCode.UNMATCHED_CREDIT: "Identify and record this income before filing - it is untracked revenue.",
    ExceptionCode.UNMATCHED_INVOICE: "Chase the client; this invoice is now past due with nothing received.",
    ExceptionCode.DUPLICATE_CREDIT: "Deduplicate before this corrupts the totals.",
    ExceptionCode.SPLIT_PAYMENT: "No action - resolved automatically across multiple credits.",
    ExceptionCode.MERGED_PAYMENT: "No action - resolved automatically across multiple invoices.",
}


@dataclass(frozen=True, slots=True)
class Exception_:
    """Deliberately not named `Exception` to avoid shadowing the builtin."""
    code: ExceptionCode
    invoice_id: str | None
    credit_id: str | None
    amount_paisa: Paisa
    explanation: str
    action: str

    @staticmethod
    def make(code: ExceptionCode, *, invoice_id: str | None, credit_id: str | None,
             amount_paisa: Paisa, explanation: str) -> "Exception_":
        return Exception_(
            code=code,
            invoice_id=invoice_id,
            credit_id=credit_id,
            amount_paisa=amount_paisa,
            explanation=explanation,
            action=ACTION_FOR_CODE[code],
        )
