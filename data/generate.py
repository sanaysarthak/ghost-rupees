"""
Seeded synthetic data generator.

Produces a Batch of clients / invoices / credits / Form26AS entries for
a financial year, with a deliberate, documented mix of clean matches
and anomalies (rate mismatches, GST omission, TDS missing from 26AS,
short payments, unmatched invoices/credits, duplicate UTRs, split and
merged payments). The mix is index-driven, not fully random, so the
exact anomaly count is known and testable - "one cherry-picked match
proves nothing" cuts both ways: the generator's own bias must be
documented too. See plan/baaki.md §10.

Two illustrative "designed" scenarios are added on top of the random
volume, including the exact worked example from plan/baaki.md §5 and
§15 (Arjun Textiles, INV-014, TDS deducted but absent from 26AS).
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from core.compose import gross_amount
from core.models import Batch, Client, Credit, DeductionKind, Form26ASEntry, Invoice, Rail
from core.money import Paisa, apply_bps, rupees_to_paisa
from core.rules.registry import resolve as resolve_ruleset

FY_START = date(2026, 4, 1)
FY_END = date(2027, 3, 31)

_CLIENT_NAMES = [
    ("Arjun Textiles Pvt Ltd", "DELA12345B"),
    ("Northwind Studios LLP", "MUMB98765C"),
    ("BluePeak Consulting", "BLRB45678D"),
    ("Fernhill Media", "CHEA23456E"),
    ("Kestrel Analytics", "PUNE34567F"),
    ("Solace Design Co", "HYDA56789G"),
    ("Ironleaf Ventures", "KOLA67890H"),
    ("Marrow Interactive", "DELB78901I"),
    ("Tidewater Labs", "MUMC89012J"),
    ("Copperline Retail", "BLRC90123K"),
]

_NARRATION_TEMPLATES = [
    "UPI/CR/{ref}/{name}/{bank}/{note}",
    "NEFT/{bank}/{ref}/{name}",
    "IMPS-{ref}-{name}-{bank}",
    "RTGS INW {ref} {name}",
    "BY TRANSFER-{name}-{ref}",
]

_BANKS = ["HDFC", "ICIC", "SBIN", "UTIB", "KKBK"]


def _random_date(rng: random.Random, start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=rng.randint(0, delta))


def _narration(rng: random.Random, name: str, ref: str, note: str = "") -> str:
    tmpl = rng.choice(_NARRATION_TEMPLATES)
    return tmpl.format(ref=ref, name=name.upper().replace(" ", ""), bank=rng.choice(_BANKS), note=note)


def _make_client(name: str, tan: str, pan_on_file: bool = True) -> Client:
    slug = "".join(ch for ch in name if ch.isalnum())[:8].lower()
    return Client(client_id=f"cli_{slug}", name=name, pan_on_file=pan_on_file, tan=tan)


def generate_batch(seed: int = 42, n_random: int = 40) -> Batch:
    rng = random.Random(seed)
    batch = Batch()

    clients = [_make_client(n, t) for n, t in _CLIENT_NAMES]
    batch.clients.extend(clients)

    inv_counter = 0
    credit_counter = 0
    entry_counter = 0

    def next_ids():
        nonlocal inv_counter, credit_counter
        inv_counter += 1
        credit_counter += 1
        return f"INV-{inv_counter:03d}", f"CR-{credit_counter:04d}"

    # ---------------------------------------------------------------
    # Random volume: clean matches, with anomalies injected by index.
    # ---------------------------------------------------------------
    for i in range(n_random):
        client = rng.choice(clients)
        issue_date = _random_date(rng, FY_START, FY_END - timedelta(days=60))
        due_date = issue_date + timedelta(days=rng.choice([15, 30, 45]))
        amount_rupees = rng.choice([8000, 12000, 15000, 18000, 20000, 25000, 32000, 45000, 60000, 75000])
        gst_applicable = rng.random() < 0.6
        kind = rng.choice([
            DeductionKind.TDS_PROFESSIONAL_194J,
            DeductionKind.TDS_PROFESSIONAL_194J,
            DeductionKind.TDS_TECHNICAL_194J,
            DeductionKind.NONE,
        ])

        inv_id, cr_id = next_ids()
        invoice = Invoice(
            invoice_id=inv_id, client_id=client.client_id, issue_date=issue_date,
            due_date=due_date, service_amount_paisa=rupees_to_paisa(str(amount_rupees)),
            gst_applicable=gst_applicable, deduction_kind=kind,
            notes=f"receipt:{inv_id}",
        )

        ruleset = resolve_ruleset(issue_date)
        rule = ruleset.rule_for(kind, pan_on_file=client.pan_on_file)
        gross = gross_amount(invoice)
        base = invoice.service_amount_paisa

        anomaly = i % 9  # deterministic anomaly cycle across the random batch

        credit_date = due_date + timedelta(days=rng.randint(0, 10))
        ref = f"{rng.randint(10**10, 10**11-1)}"

        if anomaly == 0 and rule is not None and base >= rule.threshold_paisa:
            # clean lawful deduction, present in 26AS
            ded = apply_bps(base, rule.rate_bps)
            net = Paisa(gross - ded)
            batch.form26as.append(Form26ASEntry(
                entry_id=f"26AS-{entry_counter:04d}", deductor_tan=client.tan,
                quarter=_quarter_for(issue_date), amount_paisa=ded, section=rule.legacy_citation,
            ))
            entry_counter += 1
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=net, rail=Rail.UPI,
                raw_narration=_narration(rng, client.name, ref), utr=ref,
                razorpay_payment_id=f"pay_{ref}",
            ))
            invoice = Invoice(**{**asdict(invoice), "notes": f"receipt:{inv_id};payment_id:pay_{ref}"})

        elif anomaly == 1 and rule is not None:
            # TDS deducted, correct rate, but MISSING from Form26AS (the headline finding)
            ded = apply_bps(base, rule.rate_bps)
            net = Paisa(gross - ded)
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=net, rail=Rail.UPI,
                raw_narration=_narration(rng, client.name, ref), utr=ref,
            ))

        elif anomaly == 2 and rule is not None and kind == DeductionKind.TDS_PROFESSIONAL_194J:
            # rate mismatch: paid at the technical (2%) rate instead of professional (10%)
            tech_rule = ruleset.rule_for(DeductionKind.TDS_TECHNICAL_194J, pan_on_file=client.pan_on_file)
            ded = apply_bps(base, tech_rule.rate_bps)
            net = Paisa(gross - ded)
            batch.form26as.append(Form26ASEntry(
                entry_id=f"26AS-{entry_counter:04d}", deductor_tan=client.tan,
                quarter=_quarter_for(issue_date), amount_paisa=ded, section=tech_rule.legacy_citation,
            ))
            entry_counter += 1
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=net, rail=Rail.NEFT,
                raw_narration=_narration(rng, client.name, ref), utr=ref,
            ))

        elif anomaly == 3:
            # short paid, no lawful basis
            shortfall = rupees_to_paisa(str(rng.choice([500, 1000, 1500, 2500])))
            net = Paisa(gross - shortfall)
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=net, rail=Rail.UPI,
                raw_narration=_narration(rng, client.name, ref, "partial"), utr=ref,
            ))

        elif anomaly == 4:
            # unmatched invoice - no credit at all
            credit_counter -= 1  # id not used

        elif anomaly == 5:
            # unmatched credit - a personal transfer with no invoice behind it
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=rupees_to_paisa(str(rng.choice([500, 1200, 3000]))),
                rail=Rail.UPI, raw_narration=_narration(rng, "FRIEND", ref, "personal"), utr=ref,
            ))
            # invoice itself paid cleanly and separately below
            _pay_clean(batch, rng, invoice, ruleset, client, credit_date, entry_counter)
            entry_counter += 1

        elif anomaly == 6 and invoice.gst_applicable and rule is not None:
            # GST omitted entirely, correct TDS on base only
            ded = apply_bps(base, rule.rate_bps)
            net = Paisa(base - ded)
            batch.form26as.append(Form26ASEntry(
                entry_id=f"26AS-{entry_counter:04d}", deductor_tan=client.tan,
                quarter=_quarter_for(issue_date), amount_paisa=ded, section=rule.legacy_citation,
            ))
            entry_counter += 1
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=net, rail=Rail.UPI,
                raw_narration=_narration(rng, client.name, ref, "no-gst"), utr=ref,
            ))

        elif anomaly == 7:
            # duplicate credit: same UTR seen twice
            net = gross
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=net, rail=Rail.UPI,
                raw_narration=_narration(rng, client.name, ref), utr=ref,
            ))
            credit_counter += 1
            batch.credits.append(Credit(
                credit_id=f"CR-{credit_counter:04d}", value_date=credit_date, amount_paisa=net, rail=Rail.UPI,
                raw_narration=_narration(rng, client.name, ref, "dup"), utr=ref,
            ))

        else:
            # clean, no deduction at all
            batch.credits.append(Credit(
                credit_id=cr_id, value_date=credit_date, amount_paisa=gross, rail=Rail.UPI,
                raw_narration=_narration(rng, client.name, ref), utr=ref,
            ))

        batch.invoices.append(invoice)

    # ---------------------------------------------------------------
    # Designed scenarios (deterministic, not index-driven)
    # ---------------------------------------------------------------
    arjun = clients[0]

    # The canonical worked example from plan/baaki.md - INV-014-equivalent,
    # kept as its own named invoice so the report/video can point at it by ID.
    ghost_invoice = Invoice(
        invoice_id="INV-GHOST-01", client_id=arjun.client_id,
        issue_date=date(2026, 6, 4), due_date=date(2026, 6, 19),
        service_amount_paisa=rupees_to_paisa("20000.00"), gst_applicable=True,
        deduction_kind=DeductionKind.TDS_PROFESSIONAL_194J,
        notes="receipt:INV-GHOST-01",
    )
    ruleset = resolve_ruleset(ghost_invoice.issue_date)
    rule = ruleset.rule_for(DeductionKind.TDS_PROFESSIONAL_194J, pan_on_file=arjun.pan_on_file)
    ded = apply_bps(ghost_invoice.service_amount_paisa, rule.rate_bps)   # 2,000.00
    net = Paisa(gross_amount(ghost_invoice) - ded)                       # 21,600.00
    batch.invoices.append(ghost_invoice)
    batch.credits.append(Credit(
        credit_id="CR-GHOST-01", value_date=date(2026, 6, 22), amount_paisa=net,
        rail=Rail.UPI, raw_narration="UPI/CR/452118839021/ARJUNTEXTILES/HDFC/inv-ghost-01",
        utr="452118839021",
    ))
    # deliberately NO Form26ASEntry for this one - the headline finding

    # Split payment: one invoice, two credits
    split_client = clients[1]
    split_invoice = Invoice(
        invoice_id="INV-SPLIT-01", client_id=split_client.client_id,
        issue_date=date(2026, 7, 1), due_date=date(2026, 7, 16),
        service_amount_paisa=rupees_to_paisa("40000.00"), gst_applicable=False,
        deduction_kind=DeductionKind.NONE, notes="receipt:INV-SPLIT-01",
    )
    batch.invoices.append(split_invoice)
    batch.credits.append(Credit(
        credit_id="CR-SPLIT-01A", value_date=date(2026, 7, 18),
        amount_paisa=rupees_to_paisa("25000.00"), rail=Rail.UPI,
        raw_narration="UPI/CR/998877/NORTHWINDSTUDIOSLLP/ICIC/part1", utr="998877",
    ))
    batch.credits.append(Credit(
        credit_id="CR-SPLIT-01B", value_date=date(2026, 7, 20),
        amount_paisa=rupees_to_paisa("15000.00"), rail=Rail.UPI,
        raw_narration="UPI/CR/998878/NORTHWINDSTUDIOSLLP/ICIC/part2", utr="998878",
    ))

    batch.invoices.sort(key=lambda i: i.issue_date)
    return batch

def _pay_clean(batch: Batch, rng: random.Random, invoice: Invoice, ruleset, client: Client,
               credit_date: date, entry_counter: int) -> None:
    rule = ruleset.rule_for(invoice.deduction_kind, pan_on_file=client.pan_on_file)
    gross = gross_amount(invoice)
    ref = f"{rng.randint(10**10, 10**11-1)}"
    if rule is not None and invoice.service_amount_paisa >= rule.threshold_paisa:
        ded = apply_bps(invoice.service_amount_paisa, rule.rate_bps)
        net = Paisa(gross - ded)
        batch.form26as.append(Form26ASEntry(
            entry_id=f"26AS-extra-{entry_counter:04d}", deductor_tan=client.tan,
            quarter=_quarter_for(invoice.issue_date), amount_paisa=ded, section=rule.legacy_citation,
        ))
    else:
        net = gross
    batch.credits.append(Credit(
        credit_id=f"CR-clean-{invoice.invoice_id}", value_date=credit_date, amount_paisa=net,
        rail=Rail.UPI, raw_narration=_narration(rng, client.name, ref), utr=ref,
    ))


def _quarter_for(d: date) -> str:
    fy_start_year = d.year if d.month >= 4 else d.year - 1
    q = ((d.month - 4) % 12) // 3 + 1
    return f"Q{q}-FY{fy_start_year % 100}-{(fy_start_year + 1) % 100}"


# --- serialisation -------------------------------------------------

def _enc(obj):
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def batch_to_dict(batch: Batch) -> dict:
    def d(x):
        out = asdict(x)
        for k, v in list(out.items()):
            if isinstance(v, date):
                out[k] = v.isoformat()
        return out
    return {
        "clients": [d(c) for c in batch.clients],
        "invoices": [d(i) for i in batch.invoices],
        "credits": [d(c) for c in batch.credits],
        "form26as": [d(e) for e in batch.form26as],
    }


def write_batch(batch: Batch, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = batch_to_dict(batch)
    for name, rows in data.items():
        (out_dir / f"{name}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the Ghost Rupees synthetic batch.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--out", type=str, default="data/fixtures/golden")
    args = parser.parse_args()
    b = generate_batch(seed=args.seed, n_random=args.n)
    write_batch(b, Path(args.out))
    print(f"wrote {len(b.invoices)} invoices, {len(b.credits)} credits, "
          f"{len(b.form26as)} Form26AS entries, {len(b.clients)} clients -> {args.out}")
