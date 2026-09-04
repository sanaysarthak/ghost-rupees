"""
Runs the matcher against the 14-planted-defect holdout batch
(data/holdout.py) and reports found / wrong-code / missed per defect,
plus any false positives elsewhere in the batch. This is the eval the
track's pass bar asks for: "throughput plus measured accuracy plus an
honest exception list. One cherry-picked match proves nothing."

Usage: python eval/defects.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.compose import gross_amount        # noqa: E402
from core.match import run_matcher            # noqa: E402
from data.holdout import build_holdout_batch  # noqa: E402


def run_defect_eval() -> None:
    batch, defects = build_holdout_batch()
    ledger = run_matcher(batch)
    ledger.assert_conserves(batch.invoices, gross_amount)

    all_exceptions = list(ledger.invoice_exceptions) + list(ledger.credit_exceptions)

    found = wrong_code = missed = known_gap_hit = 0
    print(f"{'#':>3}  {'expected':<28} {'result':<10} description")
    print("-" * 100)
    for d in defects:
        # match exceptions to this defect by whichever ID(s) it names -
        # a defect can be identified by invoice_id and/or credit_id,
        # possibly comma-separated for multi-record defects (splits/merges)
        target_invoice_ids = set((d.invoice_id or "").split(",")) - {""}
        target_credit_ids = set((d.credit_id or "").split(",")) - {""}

        matches = [
            e for e in all_exceptions
            if (e.invoice_id in target_invoice_ids or e.credit_id in target_credit_ids)
        ]
        exact = [e for e in matches if e.code == d.expected_code]

        if exact:
            result = "FOUND"
            found += 1
        elif matches:
            result = f"WRONG CODE ({matches[0].code.value})"
            wrong_code += 1
            if d.known_gap:
                known_gap_hit += 1
        else:
            result = "MISSED"
            missed += 1
            if d.known_gap:
                known_gap_hit += 1

        print(f"{d.n:>3}  {d.expected_code.value:<28} {result:<10} {d.description}")
        if d.known_gap and result != "FOUND":
            print(f"      known gap: {d.known_gap}")

    total = len(defects)
    print("-" * 100)
    print(f"found: {found}/{total}   wrong-code: {wrong_code}/{total}   missed: {missed}/{total}   "
          f"(of which {known_gap_hit} are documented known gaps)")

    # false positives: exceptions on records NOT among the planted defects
    all_defect_ids = set()
    for d in defects:
        all_defect_ids |= set((d.invoice_id or "").split(","))
        all_defect_ids |= set((d.credit_id or "").split(","))
    all_defect_ids.discard("")

    false_positives = [
        e for e in all_exceptions
        if e.invoice_id not in all_defect_ids and e.credit_id not in all_defect_ids
    ]
    print(f"false positives (exceptions raised on records with no planted defect): {len(false_positives)}")
    for e in false_positives:
        print(f"  {e.code.value:<28} invoice={e.invoice_id} credit={e.credit_id} "
              f"amount_paisa={int(e.amount_paisa)}")


if __name__ == "__main__":
    run_defect_eval()
