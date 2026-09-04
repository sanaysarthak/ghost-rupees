"""
The ablation study: run the matcher with the LLM narration parser OFF
(the dumb digit-only stub) versus ON (a real Claude call per credit),
and report the difference on the deliberately-planted GARBLED-name
cross-client tie (INV-TIE-C / INV-TIE-D in data/generate.py).

Why the garbled pair, not the clean-name pair (INV-TIE-A/B): an
earlier version of this scenario used narrations that spelled the
counterparty name out in full, and it turned out core.match's own
free, deterministic substring check (tier 1 of _stage3_hypothesis's
tie-break) already resolves that case with no model involved at all -
which would have made this "ablation" prove nothing about the LLM.
INV-TIE-C/D abbreviates the name the way real bank narrations often
do ("BLUPEAK CNSLTNG" for "BluePeak Consulting"), which defeats the
substring check but is exactly the kind of thing a language model can
still recognise. See DECISIONS.md Entry 7 and
tests/test_engine.py::test_garbled_name_tie_is_declined_not_guessed_without_llm_help.

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
    line_c = next((l for l in ledger.lines if l.invoice_id == "INV-TIE-C" and l.proof), None)
    line_d = next((l for l in ledger.lines if l.invoice_id == "INV-TIE-D" and l.proof), None)
    return (line_c.proof.credit_id if line_c else None, line_d.proof.credit_id if line_d else None)


def run_ablation(live: bool = False) -> None:
    batch = generate_batch(seed=42, n_random=40)

    print("=== OFF: no narration hint at all (tier 1's free substring check still runs) ===")
    ledger_off = run_matcher(batch)
    ledger_off.assert_conserves(batch.invoices, gross_amount)
    c_off, d_off = _tie_outcome(ledger_off)
    print(f"  auto-match rate: {ledger_off.auto_match_rate_pct(batch.invoices):.2f}%")
    print(f"  INV-TIE-C -> {c_off}   INV-TIE-D -> {d_off}")
    print(f"  resolved correctly? {c_off == 'CR-TIE-C' and d_off == 'CR-TIE-D'}  "
          f"(None means: honestly declined rather than guessed - the SAFE outcome without help)")

    print("\n=== STUB: digit-only baseline (the ablation's 'off' condition) ===")
    hint_stub = _build_hint_stub(batch)
    ledger_stub = run_matcher(batch, narration_hint=hint_stub)
    ledger_stub.assert_conserves(batch.invoices, gross_amount)
    c_stub, d_stub = _tie_outcome(ledger_stub)
    print(f"  auto-match rate: {ledger_stub.auto_match_rate_pct(batch.invoices):.2f}%")
    print(f"  INV-TIE-C -> {c_stub}   INV-TIE-D -> {d_stub}")
    print(f"  resolved correctly? {c_stub == 'CR-TIE-C' and d_stub == 'CR-TIE-D'}  "
          f"(the digit-only stub extracts no counterparty either, so this stays declined too)")

    if live:
        print("\n=== ON: real Claude narration parser ===")
        try:
            hint_live = _build_hint_live(batch)
        except Exception as e:  # noqa: BLE001
            print(f"  could not run the live parser: {e}")
            return
        ledger_on = run_matcher(batch, narration_hint=hint_live)
        ledger_on.assert_conserves(batch.invoices, gross_amount)
        c_on, d_on = _tie_outcome(ledger_on)
        print(f"  auto-match rate: {ledger_on.auto_match_rate_pct(batch.invoices):.2f}%")
        print(f"  INV-TIE-C -> {c_on}   INV-TIE-D -> {d_on}")
        print(f"  resolved correctly? {c_on == 'CR-TIE-C' and d_on == 'CR-TIE-D'}  "
              f"(this is what the real narration parser is expected to fix)")
    else:
        print("\n(pass --live to also run the real Claude call - needs ANTHROPIC_API_KEY)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    run_ablation(live=args.live)
