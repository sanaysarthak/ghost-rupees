"""
The ablation study: run the matcher with the LLM narration parser OFF
(the dumb digit-only stub) versus ON (a real Claude call per credit),
and report the difference in outcome on the deliberately-planted
cross-client tie (INV-TIE-A / INV-TIE-B in data/generate.py).

This is the orchestration layer - the one place allowed to import BOTH
core/ and llm/ - core/match.py itself never imports llm/ (see
tests/test_import_boundary.py); this script is what wires the two
together, exactly as plan/baaki.md's architecture intends.

Usage:
    python eval/ablation.py            # stub only, no API key needed
    python eval/ablation.py --live     # also runs the real Claude call
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.compose import gross_amount            # noqa: E402
from core.match import run_matcher                # noqa: E402
from data.generate import generate_batch          # noqa: E402


def _build_hint_stub(batch) -> dict[str, tuple[str | None, str | None]]:
    from llm.narration import parse_narration_stub
    hint = {}
    for c in batch.credits:
        p = parse_narration_stub(c.raw_narration)
        hint[c.credit_id] = (p.counterparty, p.utr)
    return hint


def _build_hint_live(batch) -> dict[str, tuple[str | None, str | None]]:
    from llm.client import get_client
    from llm.narration import parse_narration_verified
    from llm.verify import VerificationStats

    client = get_client()   # raises LLMNotConfigured with a clear message if unset
    stats = VerificationStats()
    hint = {}
    for c in batch.credits:
        parsed = parse_narration_verified(c.raw_narration, client=client, stats=stats)
        hint[c.credit_id] = (parsed.counterparty if parsed else None, parsed.utr if parsed else None)
    print(f"  (live parser verification discard rate: {stats.discard_rate_pct}% over {stats.total} credits)")
    return hint


def _tie_outcome(ledger) -> tuple[str | None, str | None]:
    line_a = next((l for l in ledger.lines if l.invoice_id == "INV-TIE-A" and l.proof), None)
    line_b = next((l for l in ledger.lines if l.invoice_id == "INV-TIE-B" and l.proof), None)
    return (line_a.proof.credit_id if line_a else None, line_b.proof.credit_id if line_b else None)


def run_ablation(live: bool = False) -> None:
    batch = generate_batch(seed=42, n_random=40)

    print("=== OFF: no narration hint at all ===")
    ledger_off = run_matcher(batch)
    ledger_off.assert_conserves(batch.invoices, gross_amount)
    a_off, b_off = _tie_outcome(ledger_off)
    print(f"  auto-match rate: {ledger_off.auto_match_rate_pct(batch.invoices):.2f}%")
    print(f"  INV-TIE-A -> {a_off}   INV-TIE-B -> {b_off}")
    print(f"  correct? {a_off == 'CR-TIE-A' and b_off == 'CR-TIE-B'}")

    print("\n=== STUB: digit-only baseline (the ablation's 'off' condition) ===")
    hint_stub = _build_hint_stub(batch)
    ledger_stub = run_matcher(batch, narration_hint=hint_stub)
    ledger_stub.assert_conserves(batch.invoices, gross_amount)
    a_stub, b_stub = _tie_outcome(ledger_stub)
    print(f"  auto-match rate: {ledger_stub.auto_match_rate_pct(batch.invoices):.2f}%")
    print(f"  INV-TIE-A -> {a_stub}   INV-TIE-B -> {b_stub}")
    print(f"  correct? {a_stub == 'CR-TIE-A' and b_stub == 'CR-TIE-B'}")

    if live:
        print("\n=== ON: real Claude narration parser ===")
        try:
            hint_live = _build_hint_live(batch)
        except Exception as e:  # noqa: BLE001
            print(f"  could not run the live parser: {e}")
            return
        ledger_on = run_matcher(batch, narration_hint=hint_live)
        ledger_on.assert_conserves(batch.invoices, gross_amount)
        a_on, b_on = _tie_outcome(ledger_on)
        print(f"  auto-match rate: {ledger_on.auto_match_rate_pct(batch.invoices):.2f}%")
        print(f"  INV-TIE-A -> {a_on}   INV-TIE-B -> {b_on}")
        print(f"  correct? {a_on == 'CR-TIE-A' and b_on == 'CR-TIE-B'}")
    else:
        print("\n(pass --live to also run the real Claude call - needs ANTHROPIC_API_KEY)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    run_ablation(live=args.live)
