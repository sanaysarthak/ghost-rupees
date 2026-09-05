#!/usr/bin/env python
"""
Ghost Rupees CLI.

    python cli.py generate            # write the golden batch to data/fixtures/golden
    python cli.py run                 # run the matcher, print a summary, write report/out/report.html
    python cli.py run --json          # also print the full machine-readable result as JSON

Deliberately stdlib argparse, not Typer/Click - this must run on a
reviewer's machine with zero pip installs beyond the standard library
for the deterministic core. (The optional LLM layer under llm/ does
need `google-genai` + `pydantic`, and `run --with-llm` will say so plainly
if they're missing rather than failing with an import traceback.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.classify import ExceptionCode          # noqa: E402
from core.compose import gross_amount            # noqa: E402
from core.ledger import Bucket, Ledger           # noqa: E402
from core.match import run_matcher               # noqa: E402
from core.money import paisa_to_rupees_str       # noqa: E402
from data.generate import generate_batch, write_batch   # noqa: E402
from data.loader import load_batch               # noqa: E402


def cmd_generate(args: argparse.Namespace) -> None:
    batch = generate_batch(seed=args.seed, n_random=args.n)
    out_dir = ROOT / args.out
    write_batch(batch, out_dir)
    print(f"wrote {len(batch.invoices)} invoices, {len(batch.credits)} credits, "
          f"{len(batch.form26as)} Form26AS entries, {len(batch.clients)} clients -> {out_dir}")


def _print_summary(batch, ledger: Ledger) -> None:
    print(f"invoices: {len(batch.invoices)}   credits: {len(batch.credits)}   "
          f"clients: {len(batch.clients)}   Form26AS entries: {len(batch.form26as)}")
    print()
    print(f"auto-match rate:      {ledger.auto_match_rate_pct(batch.invoices):.2f}%")
    print(f"rupees accounted for: {ledger.rupees_accounted_pct(batch.invoices, gross_amount):.2f}%")
    print()
    totals = ledger.totals_by_bucket()
    for b in Bucket:
        print(f"  {b.value:24s} Rs {paisa_to_rupees_str(totals[b])}")
    print()
    from collections import Counter
    inv_codes = Counter(e.code.value for e in ledger.invoice_exceptions)
    cred_codes = Counter(e.code.value for e in ledger.credit_exceptions)
    print(f"invoice exceptions: {len(ledger.invoice_exceptions)}")
    for code, n in inv_codes.most_common():
        print(f"  {code:28s} x{n}")
    print(f"credit exceptions: {len(ledger.credit_exceptions)}")
    for code, n in cred_codes.most_common():
        print(f"  {code:28s} x{n}")
    at_risk = sum(
        int(e.amount_paisa) for e in ledger.invoice_exceptions
        if e.code in (ExceptionCode.TDS_NOT_IN_26AS, ExceptionCode.SHORT_PAID, ExceptionCode.TDS_RATE_MISMATCH)
    )
    print()
    print(f"rupees at risk (TDS not in 26AS + rate mismatches + short-paid): Rs {paisa_to_rupees_str(at_risk)}")


def cmd_run(args: argparse.Namespace) -> None:
    in_dir = ROOT / args.data
    if in_dir.exists() and any(in_dir.glob("*.json")):
        batch = load_batch(in_dir)
        print(f"loaded batch from {in_dir}")
    else:
        print(f"{in_dir} not found or empty - generating in-memory instead "
              f"(run `python cli.py generate` to commit fixtures to disk)")
        batch = generate_batch(seed=args.seed, n_random=args.n)

    ledger = run_matcher(batch)
    try:
        ledger.assert_conserves(batch.invoices, gross_amount)
        print("CONSERVATION CHECK: PASS - every invoice's buckets sum exactly to its gross amount.\n")
    except Exception as e:  # noqa: BLE001 - this is the one place we want to surface it loudly
        print("CONSERVATION CHECK: FAIL")
        print(str(e))
        sys.exit(1)

    _print_summary(batch, ledger)

    if args.report:
        from report.build import build_report
        out_path = ROOT / args.report
        build_report(batch, ledger, out_path)
        print(f"\nreport written to {out_path}")

    if args.json:
        import json
        payload = {
            "auto_match_rate_pct": ledger.auto_match_rate_pct(batch.invoices),
            "rupees_accounted_pct": ledger.rupees_accounted_pct(batch.invoices, gross_amount),
            "invoice_exceptions": [
                {"code": e.code.value, "invoice_id": e.invoice_id, "credit_id": e.credit_id,
                 "amount_paisa": int(e.amount_paisa), "explanation": e.explanation, "action": e.action}
                for e in ledger.invoice_exceptions
            ],
            "credit_exceptions": [
                {"code": e.code.value, "invoice_id": e.invoice_id, "credit_id": e.credit_id,
                 "amount_paisa": int(e.amount_paisa), "explanation": e.explanation, "action": e.action}
                for e in ledger.credit_exceptions
            ],
        }
        print()
        print(json.dumps(payload, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="ghost-rupees", description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate the synthetic batch and write it to disk")
    p_gen.add_argument("--seed", type=int, default=42)
    p_gen.add_argument("--n", type=int, default=60)
    p_gen.add_argument("--out", type=str, default="data/fixtures/golden")
    p_gen.set_defaults(func=cmd_generate)

    p_run = sub.add_parser("run", help="run the matcher and print a summary")
    p_run.add_argument("--data", type=str, default="data/fixtures/golden",
                        help="directory of a previously-generated batch (falls back to in-memory generation)")
    p_run.add_argument("--seed", type=int, default=42)
    p_run.add_argument("--n", type=int, default=60)
    p_run.add_argument("--json", action="store_true", help="also print the full result as JSON")
    p_run.add_argument("--report", type=str, default="report/out/report.html",
                        help="path to write the self-contained HTML report (empty string to skip)")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
