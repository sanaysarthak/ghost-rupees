"""Load a written batch (data/generate.py output) back into dataclasses."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from core.models import Batch, Client, Credit, DeductionKind, Form26ASEntry, Invoice, Rail


def _d(s: str) -> date:
    return date.fromisoformat(s)


def load_batch(in_dir: Path) -> Batch:
    in_dir = Path(in_dir)
    clients_raw = json.loads((in_dir / "clients.json").read_text(encoding="utf-8"))
    invoices_raw = json.loads((in_dir / "invoices.json").read_text(encoding="utf-8"))
    credits_raw = json.loads((in_dir / "credits.json").read_text(encoding="utf-8"))
    form26as_raw = json.loads((in_dir / "form26as.json").read_text(encoding="utf-8"))

    batch = Batch()
    batch.clients = [Client(**c) for c in clients_raw]
    batch.invoices = [
        Invoice(
            invoice_id=i["invoice_id"], client_id=i["client_id"],
            issue_date=_d(i["issue_date"]), due_date=_d(i["due_date"]),
            service_amount_paisa=i["service_amount_paisa"], gst_applicable=i["gst_applicable"],
            deduction_kind=DeductionKind(i["deduction_kind"]), notes=i.get("notes", ""),
        )
        for i in invoices_raw
    ]
    batch.credits = [
        Credit(
            credit_id=c["credit_id"], value_date=_d(c["value_date"]),
            amount_paisa=c["amount_paisa"], rail=Rail(c["rail"]),
            raw_narration=c["raw_narration"], utr=c.get("utr"),
            razorpay_payment_id=c.get("razorpay_payment_id"),
            razorpay_customer_identifier=c.get("razorpay_customer_identifier"),
        )
        for c in credits_raw
    ]
    batch.form26as = [Form26ASEntry(**e) for e in form26as_raw]
    return batch
