# Ghost Rupees

Every rupee you invoiced — received, deducted, or still owed.

Razorpay AI Buildathon 2026 · Track 4, AI Finance Controller

---

## The number

On a 66-invoice synthetic batch, Ghost Rupees auto-matched **75.76%** of
invoices to a bank/gateway credit with zero manual intervention, and
accounted for **100.00%** of the money by construction — every rupee
invoiced lands in one of exactly four buckets (received, lawfully
deducted and creditable, deducted but **absent from Form 26AS**, or
short), and those four are asserted to sum to the invoiced total in
integer paisa on every run. Along the way it found **Rs 36,575.82**
sitting in TDS deductions that were never deposited with the
government, rate mismatches that were never corrected, and short
payments with no lawful basis — money a freelancer or small business
would otherwise simply never notice was missing. Separately, on a
14-defect held-out eval batch with known ground truth
(`python eval/defects.py`), it correctly classified **13 of 14**
planted defects, with the 14th an honestly documented known gap (FX
spread handling isn't built yet) rather than a silent miss.

The auto-match rate is deliberately not the highest number this engine
could report — it declines to guess rather than silently resolving an
ambiguous tie by chance (see `DECISIONS.md` Entry 7). A lower number
that can be trusted beats a higher one that occasionally can't.

## 60-second run

```
git clone <this repo>
cd ghost-rupees
python cli.py run
```

No API key needed, no `pip install` needed — the deterministic engine
is pure standard library. That one command loads the committed golden
batch (`data/fixtures/golden/`), runs the matcher, prints a summary,
asserts the conservation law (the run fails loudly if it doesn't
balance), and writes a self-contained HTML report to
`report/out/report.html` — open it directly if you'd rather not run
anything at all; it's committed at that path already.

```
python cli.py generate      # regenerate the synthetic batch from scratch (seeded, deterministic)
python -m pytest -q         # 47 tests, ~0.5s, no network
python eval/ablation.py     # the narration-parser ablation (stub only by default; --live needs GEMINI_API_KEY)
python eval/defects.py      # the 14-planted-defect held-out eval, with known ground truth
```

## What it does

A freelancer/small business invoices clients. Money arrives net of
deductions nobody explains. Ghost Rupees reconciles three sources that
nothing today joins: invoices raised, credits actually received
(UPI/NEFT/IMPS/RTGS/Razorpay), and TDS actually reported in Form 26AS.
Every invoice's gross amount is decomposed into exactly four buckets —
**RECEIVED**, **DEDUCTED_CREDITABLE**, **DEDUCTED_UNCREDITABLE**,
**SHORT** — asserted to sum to the invoiced total in integer paisa. The
headline bucket, `DEDUCTED_UNCREDITABLE`, is money that was deducted
from you and never deposited with the government — permanently gone
unless you notice and chase it. Nothing else in this space computes
that number.

## Results (golden batch, seed=42, n=60 random + 6 designed scenarios)

| Metric | Value |
|---|---|
| Invoices / credits / clients | 66 / 73 / 10 |
| Form 26AS entries | 13 |
| Auto-match rate | 75.76% |
| Rupees accounted for | 100.00% (by construction — see `core/ledger.py`) |
| Rupees at risk (TDS not in 26AS + rate mismatches + short-paid) | Rs 36,575.82 |
| 14-defect held-out eval | 13/14 correctly classified, 0 false positives, 1 documented known gap |

Invoice exceptions by code: `UNMATCHED_INVOICE` ×10 · `TDS_NOT_IN_26AS`
×8 · `SHORT_PAID` ×6 · `TDS_RATE_MISMATCH` ×3 · `GST_OMITTED` ×3 ·
`TDS_BELOW_THRESHOLD` ×1 · `SPLIT_PAYMENT` ×1.
Credit exceptions: `UNMATCHED_CREDIT` ×10 · `DUPLICATE_CREDIT` ×6.

**The ablation** (`python eval/ablation.py`): two different clients
(BluePeak Consulting, Fernhill Media) deliberately invoice the
identical amount, paid via generic UPI credits with no Razorpay
identifiers and no UTR either invoice references, with the counterparty
name abbreviated in the narration the way real bank feeds often
truncate it ("BLUPEAK CNSLTNG" for "BluePeak Consulting" — see
`INV-TIE-C`/`INV-TIE-D`). Amount+date matching alone cannot tell the
two credits apart, and the abbreviation defeats a plain substring
check too. **Without narration understanding, the matcher correctly
declines to guess** rather than silently swapping the two payments —
both invoices come back honestly unresolved. Given the parsed
counterparty name from job 1 (`llm.narration`), the tie resolves
correctly for both. The interesting part isn't "the deterministic
engine gets it wrong without AI" — it's safe by default — it's that
**the LLM converts an honest "I don't know" into a correct "I know,"**
improving coverage without ever risking a silent wrong match. See
`DECISIONS.md` Entry 7 for the full story (including an earlier,
cleaner-named version of this scenario that a later deterministic fix
accidentally solved for free, which is why it isn't the one shown
here), and
`tests/test_engine.py::test_garbled_name_tie_is_declined_not_guessed_without_llm_help`
/ `test_narration_hint_resolves_a_tie_the_substring_check_cannot` for
the pinned-down proof.

## Architecture

```
ghost-rupees/
├── core/          deterministic engine - NEVER imports llm/ (enforced by
│                  tests/test_import_boundary.py, an AST walk, not a convention)
│   ├── money.py       integer paisa everywhere, floats rejected outright
│   ├── models.py      Invoice, Credit, Deduction-bearing types, Form26ASEntry, Client
│   ├── rules/         FY-versioned TDS/GST rate tables, cited, cumulative-threshold aware
│   ├── compose.py     the hypothesis solver - predicts net under each lawful/common-error deduction
│   ├── match.py       the matcher: identity, UTR, hypothesis (3-tier tie-break), split/merge, short-pay, classify
│   ├── classify.py    15-code typed exception taxonomy
│   ├── proof.py       an audit record on every matched decision
│   └── ledger.py      the conservation law: 4 buckets, asserted to sum to gross, always
├── llm/           optional - narration parsing, cross-client tie-break input, exception prose.
│                  Every narration result passes llm.verify.gate_narration before being trusted.
├── data/          seeded synthetic generator, the committed golden batch, and the 14-defect holdout (data/holdout.py)
├── eval/          eval/ablation.py (narration-parser on/off) + eval/defects.py (the 14-defect held-out eval)
├── report/        self-contained HTML report builder
└── cli.py         the one-command entry point (stdlib argparse, zero deps for the core path)
```

**The trust boundary, in one sentence:** a model that's 99% right about
money is a 1% embezzlement rate, so the model never touches the money —
it only reads the messy English on a bank narration, and even then
every claimed fact is checked against the raw source text before
anything downstream trusts it.

## Where AI is deliberately NOT used

- All arithmetic — integer paisa throughout (`core/money.py` refuses a
  raw `float` outright)
- All TDS/GST rate and threshold application — from versioned,
  cited tables (`core/rules/registry.py`), including the cumulative
  per-FY threshold logic
- The matched/unmatched verdict itself, and which of the four buckets
  an amount falls into
- The Form 26AS cross-check
- Any decision above a rupee threshold — the model proposes a
  disambiguation, it never commits one silently

Where AI genuinely earns its place: turning free-text bank narration
into structured fields no regex reliably handles, and using the
counterparty name recovered from that text to break a same-amount,
same-window tie the deterministic matcher's own free tier-1 substring
check couldn't (see the ablation above). Note the escalation order in
`core/match.py`'s tie-break: try a free deterministic check first, only
reach for the model when that genuinely fails, and if neither resolves
it, decline rather than guess — the "right tool in the right place"
line applied literally, three tiers deep.

## The rule tables

See `plan/baaki.md` §7 for the full table, and its Verification log for
the row-by-row detail. Re-verified 5 September 2026 against multiple
independent sources per row (not just one), specifically checking exact
numbers and effective dates rather than trusting a general topic match.
Two real issues were found this way and fixed: the no-PAN override rate
was wrong for 194-O specifically (was applying the generic 20% instead
of a documented 5% carve-out - see `DECISIONS.md` Entry 9), and the
194-O threshold's entity-type scope (individual/HUF only) was true but
previously unstated. The Income Tax Act 2025 → Section 393 citation
change - the boldest, most checkable-but-easy-to-fabricate claim in the
table - was independently confirmed real across three sources. The
official incometaxindia.gov.in FAQ pages block automated fetches
(403), so this is strong secondary-source corroboration, not primary
government-document confirmation - stated plainly, not overclaimed.

## Limitations (honest, not hidden)

- Threshold aggregation (Section 194J's Rs 50,000/FY cumulative
  threshold) is computed by summing this project's own invoices for a
  payee in the financial year — correct for this closed-world synthetic
  batch, but in production the authoritative source would be the
  payee's actual cumulative-TDS ledger, not a sum of invoices we
  happen to know about.
- Stage 2 (UTR lookup via Razorpay's Fetch-Payments-Using-UTR API)
  takes an injectable resolver that is a documented no-op by default —
  wiring it to live Razorpay credentials is a one-function change, left
  for when real test-mode credentials are available.
- Stage 4 set-matching (split/merged payments) is bounded to combinations
  of up to 3 credits/invoices — a documented cap, not silently unlimited.
- `FX_SPREAD_UNEXPLAINED` (a foreign-wire FX-spread shortfall) has no
  dedicated hypothesis yet — the 14-defect eval's one honest miss (see
  `eval/defects.py`'s output and `data/holdout.py`'s `known_gap` field).
  `OVER_PAID` and `GATEWAY_FEE_VARIANCE` were in the same state until
  Entry 8 — both now have real matcher support and are caught by the
  eval.
- The Smart Collect A/B run (real Razorpay test-mode credentials
  collecting the same transactions via anonymous UPI vs. Smart Collect
  identifiers, per `plan/baaki.md` §4) needs a real Razorpay test
  account and hasn't been run yet — the next concrete piece of work.
- The 9 tax rows in `plan/baaki.md` §7 are still only cross-checked
  against secondary sources, not the Income Tax Department's own
  primary publication.

## What broke

See `DECISIONS.md` — kept live, not reconstructed after the fact.
Highlights: the 194J threshold turned out to be cumulative-per-FY, not
per-invoice (Entry 2); the synthetic generator's own round-number
amounts were manufacturing fake collisions between unrelated clients
(Entry 3); a genuine, conservation-law-invisible identity swap between
two clients with identical invoice amounts (Entry 4); several
exception codes (`SHORT_PAID`, `MERGED_PAYMENT`,
`PLATFORM_COMMISSION_VARIANCE`, `OVER_PAID`, `GATEWAY_FEE_VARIANCE`)
that were defined but never actually assigned by the matcher (Entries
6-8); and building the 14-defect eval surfacing a chain of matching
bugs that ultimately led to a real design principle — the matcher
declines an ambiguous match rather than guessing, the same discipline
already applied to the LLM layer, now applied to the deterministic
core's own tie-breaks too (Entry 7).
