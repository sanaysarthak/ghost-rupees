"""
The Smart Collect A/B: generates the SAME underlying transactions
twice - once as anonymous UPI credits (the status quo for most
independent earners, who collect into a personal bank account), once
as if collected through a Razorpay Smart Collect customer identifier
(a dedicated virtual account per client) - and compares the matcher's
auto-match rate between the two.

This is the argument for "Razorpay is essential, not decorative" made
as a measured number rather than a claim: when every credit already
carries a razorpay_customer_identifier tying it to a specific client,
core.match's stage 1 (identity) resolves it with certainty, with no
amount/date guesswork and no chance of a cross-client collision at
all - the exact class of bug this project spent real time fixing (see
DECISIONS.md Entries 4 and 7) simply cannot occur in the Smart Collect
run, because the ambiguity it exploits never exists in the first
place.

HONESTY NOTE: this test account does not have the Smart Collect
product enabled (confirmed live 2026-09-05 - POST /v1/virtual_accounts
returns "The requested URL was not found on the server", Razorpay's
error for a product not turned on for this merchant, not a wrong
endpoint - see data/fetch_razorpay_fixtures.py and DECISIONS.md Entry
12). The Smart Collect identifier format used below
("va_<15 hex chars>", matching the real "va_..." ID prefix Razorpay
uses for virtual accounts) is modelled on the officially documented
request/response schema (razorpay.com/docs/api/payments/smart-collect/
create-cust-id-bank-account/), NOT a live API response - unlike the 15
fixtures in data/fixtures/razorpay_raw/, which are real. The matching
LOGIC this script measures (core.match's stage-1 identity resolution)
is real, tested, and already proven on the live golden batch; only the
Smart Collect *identifier strings themselves* are synthetic here,
because generating real ones needs a product toggle only the account
owner can enable in the Razorpay dashboard.

Usage: python eval/smart_collect_ab.py
"""

from __future__ import annotations

import random
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.compose import gross_amount               # noqa: E402
from core.match import run_matcher                    # noqa: E402
from core.models import Batch, Client, Credit, DeductionKind, Invoice, Rail   # noqa: E402
from core.money import rupees_to_paisa                # noqa: E402

_CLIENTS = [
    ("Arjun Textiles Pvt Ltd", "cli_va01"),
    ("Northwind Studios LLP", "cli_va02"),
    ("BluePeak Consulting", "cli_va03"),
    ("Fernhill Media", "cli_va04"),
    ("Kestrel Analytics", "cli_va05"),
    ("Solace Design Co", "cli_va06"),
    ("Ironleaf Ventures", "cli_va07"),
    ("Marrow Interactive", "cli_va08"),
]

_BANKS = ["HDFC", "ICIC", "SBIN", "UTIB", "KKBK"]


def _va_id(rng: random.Random) -> str:
    """Synthetic Smart Collect identifier in Razorpay's real "va_..." ID
    format - see this module's docstring for why it's synthetic here."""
    return "va_" + "".join(rng.choice("0123456789abcdef") for _ in range(15))


def _build_common_batch(seed: int, n: int) -> tuple[Batch, list[tuple[Invoice, int]]]:
    """
    Builds the shared client/invoice set both runs start from, plus
    the (invoice, net_amount_paisa) pairs credits will be generated for.
    No deductions in this batch - the point is isolating identity
    resolution, not re-testing the TDS logic already covered elsewhere.

    Every 4th invoice is deliberately given the SAME amount and a
    nearby date as the invoice before it, for a DIFFERENT client -
    exactly the real-world situation that makes anonymous UPI
    reconciliation genuinely hard (two different clients happening to
    pay similar round-ish amounts in the same week) and that a Smart
    Collect identifier makes a non-issue, because identity no longer
    depends on the amount at all.
    """
    rng = random.Random(seed)
    batch = Batch()
    clients = [Client(client_id=cid, name=name) for name, cid in _CLIENTS]
    batch.clients.extend(clients)

    pairs = []
    prev_amount = None
    prev_date = None
    for i in range(n):
        client = rng.choice(clients)
        if i % 4 == 3 and prev_amount is not None:
            # deliberate collision: same amount, date within a few days
            # of the previous invoice, different client
            amount = prev_amount
            issue_date = prev_date + timedelta(days=rng.randint(1, 3))
        else:
            issue_date = date(2026, 4, 1) + timedelta(days=rng.randint(0, 300))
            amount = rupees_to_paisa(str(rng.randrange(6_500, 92_000, 137)))
        due_date = issue_date + timedelta(days=rng.choice([15, 30]))
        inv = Invoice(
            invoice_id=f"AB-{i:03d}", client_id=client.client_id,
            issue_date=issue_date, due_date=due_date,
            service_amount_paisa=amount, gst_applicable=False,
            deduction_kind=DeductionKind.NONE, notes=f"receipt:AB-{i:03d}",
        )
        batch.invoices.append(inv)
        pairs.append((inv, int(gross_amount(inv))))
        prev_amount, prev_date = amount, issue_date
    return batch, pairs


def build_run_a_anonymous_upi(seed: int, n: int) -> Batch:
    """
    Status quo: payments arrive as bank-statement-style credits with NO
    payer name in the narration at all - just a rail and a reference
    number. This is deliberately the harder, but realistic, case: a
    UPI app notification sometimes does show a name, but a bank
    statement CSV export (what most independent earners actually
    reconcile against at month-end) commonly does not, and NEFT/RTGS
    narrations are frequently just bank codes and a UTR. Identity has
    to come from amount+date alone - exactly the situation Smart
    Collect's own identifier removes entirely (Run B below).
    """
    rng = random.Random(seed)
    batch, pairs = _build_common_batch(seed, n)
    for inv, net in pairs:
        ref = str(rng.randint(10**10, 10**11 - 1))
        narration = f"{rng.choice(['UPI', 'NEFT', 'IMPS', 'RTGS'])}/CR/{ref}/{rng.choice(_BANKS)}"
        credit_date = inv.due_date + timedelta(days=rng.randint(0, 10))
        batch.credits.append(Credit(
            credit_id=f"ABCR-{inv.invoice_id}", value_date=credit_date,
            amount_paisa=net, rail=Rail.UPI, raw_narration=narration, utr=ref,
        ))
    return batch


def build_run_b_smart_collect(seed: int, n: int) -> Batch:
    """Same invoices, same amounts, same dates - but each client has its
    own Smart Collect identifier, and every credit carries it. Stage 1
    (identity) resolves these with certainty; amount/date ambiguity
    cannot arise because the credit names its own client directly."""
    rng = random.Random(seed)
    batch, pairs = _build_common_batch(seed, n)
    va_by_client = {c.client_id: _va_id(rng) for c in batch.clients}

    updated_invoices = []
    for inv, net in pairs:
        va = va_by_client[inv.client_id]
        updated_invoices.append(replace(inv, notes=f"receipt:{inv.invoice_id};customer_identifier:{va}"))
        credit_date = inv.due_date + timedelta(days=rng.randint(0, 10))
        ref = str(rng.randint(10**10, 10**11 - 1))
        client_name = batch.client(inv.client_id).name.upper().replace(" ", "")
        batch.credits.append(Credit(
            credit_id=f"ABCR-{inv.invoice_id}", value_date=credit_date,
            amount_paisa=net, rail=Rail.RAZORPAY_SMART_COLLECT,
            raw_narration=f"SMARTCOLLECT/{va}/{client_name}", utr=None,
            razorpay_customer_identifier=va,
        ))
    batch.invoices = updated_invoices
    return batch


def run_ab(seed: int = 7, n: int = 40) -> None:
    batch_a = build_run_a_anonymous_upi(seed, n)
    ledger_a = run_matcher(batch_a)
    ledger_a.assert_conserves(batch_a.invoices, gross_amount)
    rate_a = ledger_a.auto_match_rate_pct(batch_a.invoices)

    batch_b = build_run_b_smart_collect(seed, n)
    ledger_b = run_matcher(batch_b)
    ledger_b.assert_conserves(batch_b.invoices, gross_amount)
    rate_b = ledger_b.auto_match_rate_pct(batch_b.invoices)

    identity_matches_b = sum(
        1 for l in ledger_b.lines
        if l.proof and l.proof.stage == "stage1_identity"
    )

    print(f"Run A - anonymous UPI credits ({n} invoices, {len(batch_a.clients)} clients):")
    print(f"  auto-match rate: {rate_a:.2f}%")
    print()
    print(f"Run B - Razorpay Smart Collect identifiers ({n} invoices, {len(batch_b.clients)} clients):")
    print(f"  auto-match rate: {rate_b:.2f}%")
    print(f"  resolved via certain stage-1 identity: {identity_matches_b}/{n}")
    print()
    print(f"delta: {rate_b - rate_a:+.2f} percentage points")
    print()
    print("Same invoices, same amounts, same dates, same clients - the only "
          "difference is whether the credit identifies its own payer. That gap "
          "is the cost of collecting into a personal account instead of a "
          "Razorpay Smart Collect identifier.")


if __name__ == "__main__":
    run_ab()
