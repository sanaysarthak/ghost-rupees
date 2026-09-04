"""Core data model: Invoice, Credit, Deduction, Form26ASEntry, Client.

Plain dataclasses, no ORM - the batch (a few hundred records) fits in
memory. Every money field is Paisa (core.money.Paisa), never a float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from core.money import Paisa


class Rail(str, Enum):
    UPI = "UPI"
    IMPS = "IMPS"
    NEFT = "NEFT"
    RTGS = "RTGS"
    CARD = "CARD"
    WIRE = "WIRE"
    RAZORPAY_SMART_COLLECT = "RAZORPAY_SMART_COLLECT"
    RAZORPAY_PAYMENT_LINK = "RAZORPAY_PAYMENT_LINK"


class DeductionKind(str, Enum):
    TDS_PROFESSIONAL_194J = "TDS_PROFESSIONAL_194J"
    TDS_TECHNICAL_194J = "TDS_TECHNICAL_194J"
    TDS_CONTRACT_194C = "TDS_CONTRACT_194C"
    TDS_COMMISSION_194H = "TDS_COMMISSION_194H"
    TDS_ECOMMERCE_194O = "TDS_ECOMMERCE_194O"
    PLATFORM_COMMISSION = "PLATFORM_COMMISSION"
    GATEWAY_FEE = "GATEWAY_FEE"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class Client:
    client_id: str
    name: str
    pan_on_file: bool = True
    tan: str | None = None            # deductor's TAN, for 26AS cross-check
    contracted_commission_bps: int | None = None  # for platform clients
    contracted_mdr_bps: int | None = None          # for gateway/payment-aggregator clients


@dataclass(frozen=True, slots=True)
class Invoice:
    invoice_id: str
    client_id: str
    issue_date: date
    due_date: date
    service_amount_paisa: Paisa       # the base amount, before GST
    gst_applicable: bool              # False for export-of-services (LUT, 0%)
    deduction_kind: DeductionKind     # what the invoice EXPECTS to be deducted, if any
    notes: str = ""                   # may carry a Razorpay receipt/payment_id for stage-1 match


@dataclass(frozen=True, slots=True)
class Credit:
    credit_id: str
    value_date: date
    amount_paisa: Paisa
    rail: Rail
    raw_narration: str                 # the messy bank/gateway text
    utr: str | None = None             # normalised UTR if already known
    razorpay_payment_id: str | None = None
    razorpay_customer_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class Form26ASEntry:
    entry_id: str
    deductor_tan: str
    quarter: str            # e.g. "Q1-FY2026-27"
    amount_paisa: Paisa
    section: str             # e.g. "194J" or the s.393 table item


@dataclass
class Batch:
    """One reconciliation run's full input set."""
    clients: list[Client] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)
    form26as: list[Form26ASEntry] = field(default_factory=list)

    def client(self, client_id: str) -> Client:
        for c in self.clients:
            if c.client_id == client_id:
                return c
        raise KeyError(f"unknown client_id {client_id!r}")
