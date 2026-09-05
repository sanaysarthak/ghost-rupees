# Decisions and what broke

Kept as it happens, not reconstructed later. See plan/baaki.md §16 for
how this feeds the application form's "Build Challenges & Technical
Obstacles" field.

## Entry 1 — 2026-09-03 — why Track 4, why this problem

Picked Track 4 (AI Finance Controller) over the more obviously "AI"
tracks (Growth & Agentic Commerce, Revenue Recovery) deliberately.
Razorpay already ships Vulcan (a production payments foundation model)
and Agent Studio (an agent marketplace doing cart recovery, dispute
resolution, cashflow prediction). Building a weaker student copy of
either loses before it starts. Track 4 is the only track whose brief
explicitly sanctions synthetic data, and its own "why now" line -
"verification capacity, not generation speed, is the bottleneck" - is
close to a direct spec for this project.

The problem itself is personal, not researched: as a freelancer, an
invoice for Rs 20,000 arriving as Rs 18,000 with no explanation is a
completely normal, completely unexplained experience. Nobody currently
tells you whether that gap was lawful TDS, a client short-paying you,
or TDS that was withheld and never actually deposited with the
government - the last of which is money you cannot ever get back
unless you notice and chase it.

Deliberately NOT building: a chat interface over the reconciliation
(would convert a verification engine into a chatbot and throw away the
whole differentiation), real bank/account-aggregator integration, or
anything that touches actual ITR filing.

## Entry 2 — 2026-09-03 — the 194J threshold is cumulative, not per-invoice

Built the first version of `core.compose.hypotheses_for_invoice` with a
naive per-invoice threshold check: `base >= rule.threshold_paisa`. A
test asserting the canonical worked example (Rs 20,000 invoice, correct
10% TDS) should be flagged `lawful=True` failed - because Rs 20,000
alone is below the Rs 50,000 194J threshold.

That's not a test bug, it's a real fact about how the threshold works:
Section 194J's Rs 50,000 threshold applies cumulatively per payee per
financial year, not per invoice. A client who has already paid you
Rs 40,000 this FY is required to deduct TDS on your next invoice even
if that invoice alone is small - and many real deductors do apply the
threshold this way (correctly), which is exactly why a naive
per-invoice check would have made GhostRupees wrongly flag lawful
deductions as anomalies.

Fix: `hypotheses_for_invoice` now takes a `prior_base_paisa_this_fy`
parameter, and `core.match` computes it by summing the same client's
earlier invoices in the same financial year before calling it. Two new
tests (`test_lawful_correct_is_marked_lawful_when_fy_threshold_already_crossed`
/ `..._unlawful_when_threshold_not_yet_crossed`) pin both sides of this
down explicitly.

Known simplification still in the code: threshold aggregation is
computed by literally summing prior invoices in the batch, which is
correct for this project's closed-world synthetic data but would need
to read the payee's real cumulative-TDS ledger in production (Form
26AS's own running totals would be the authoritative source, not a
sum of *our* invoices, which might not even be complete from the
deductor's side).

## Entry 3 — 2026-09-03 — the generator's own amount choices were breaking the matcher

First full run of Gate 1 passed (conservation held), but the auto-match
rate was a disappointing 72.58% with the golden batch. Assumed the
matcher was weak and started auditing `core.match` stage by stage.

The matcher was fine. The generator was drawing invoice amounts from a
fixed set of only 10 round rupee values (8000, 12000, ... 75000) across
60 invoices. With that few distinct values, completely unrelated
clients' invoices routinely predicted the identical net paisa amount,
and `core.match`'s stage 3 searches the *entire* credit pool for an
amount match within the date window - by design, since in real life
your bank account receives money from every client into the same pool
and the tool has to be able to tell them apart. Once two unrelated
credits from different clients had the exact same amount and
overlapping date windows, whichever the matcher checked first would
"win," starving the correct invoice of its own credit and manufacturing
an artificial UNMATCHED_INVOICE.

Fix: widened the amount generator to `rng.randrange(6_500, 92_000, 137)`
- an irregular step that makes exact collisions rare, which is also
just more realistic (real invoices are rarely round thousands repeated
across dozens of clients). Auto-match rate went from 72.58% to 82.26%
on the same seed, purely from more realistic input data - the matcher
code did not change.

This is now deliberately exploited on purpose in the next entry, rather
than being an accident to eliminate entirely.

## Entry 4 — 2026-09-03 — a silent identity swap that conservation cannot catch

Once collisions were mostly eliminated (entry 3), it became clear that
*rare, deliberate* collisions are actually the most important case to
handle well - not eliminate. Added two designed invoices (INV-TIE-A,
INV-TIE-B) for two different clients with the identical invoiced
amount, paid via generic UPI credits carrying no Razorpay identifiers
and no UTR either invoice references.

Result: without any way to distinguish the two credits beyond amount
and date, `core.match` picks whichever credit it happens to encounter
first - and confirmed empirically that this produces a **wrong** match:
INV-TIE-A gets attributed to Fernhill Media's payment and INV-TIE-B to
BluePeak's, exactly swapped. `ledger.assert_conserves()` still passes
without complaint, because both invoices are still fully accounted for
in aggregate - the conservation law has no way to know WHOSE money it
is, only that the totals add up. That is a real, dangerous class of
bug: a reconciliation tool can report a perfectly clean run while
silently crediting the wrong client.

This is exactly the gap job 1 (narration parsing) exists to close: the
free-text counterparty name in the raw narration is information the
deterministic amount/date matcher never reads. Extended
`core.match._stage3_hypothesis` to accept an optional `narration_hint`
- a plain `{credit_id: (counterparty, utr)}` dict built entirely
*outside* `core/` (in `eval/ablation.py`) from `llm.narration`'s
output, specifically so `core/` never has to import `llm/` to use it
(`tests/test_import_boundary.py` enforces this). With a correct hint,
both invoices resolve to the right credit; with the deliberately dumb
`parse_narration_stub` (a digit-only regex baseline, no counterparty
extraction at all), the swap still happens - which is the honest "off"
condition for the ablation in `eval/ablation.py`.

This also reframed the whole project's Gate 1 metric for me: "does the
ledger balance" is necessary but not sufficient. Whether a matched
result is right about *who* it belongs to needed its own test
(`test_cross_client_tie_is_wrong_without_a_narration_hint` /
`test_narration_hint_resolves_the_cross_client_tie_correctly`), because
nothing about the conservation law would ever have caught it.

## Entry 5 — 2026-09-03 — where AI is deliberately NOT used

Every rate, threshold, GST calculation, and the matched/unmatched
verdict itself is pure deterministic Python (`core/`) - the LLM layer
(`llm/`) is called for exactly three things: parsing messy bank
narration text into structured fields, breaking cross-client ties using
the parsed counterparty name (see Entry 4), and writing the
human-readable explanation/chase-message prose for an exception whose
code, amount, and cause were already computed deterministically. Every
LLM output for narration passes through `llm.verify.gate_narration`
before anything downstream trusts it - a claimed UTR that doesn't
literally appear in the source text is discarded, not corrected or
guessed at.

## Entry 6 — 2026-09-03 — SHORT_PAID was defined but never actually assigned

`core.classify.ExceptionCode.SHORT_PAID` existed in the taxonomy since
the first version of `core/classify.py`, with an action string and
everything - but a `grep` for it in `core/match.py` turned up nothing.
Every invoice paid short with no lawful deduction basis (the
generator's own anomaly==3 case: an arbitrary partial payment) was
falling through stage 3's exact-hypothesis matching (correctly, since a
short-pay by definition matches no modelled hypothesis) straight into
the final catch-all: the ENTIRE gross amount booked to SHORT and
classified as `UNMATCHED_INVOICE`, discarding the fact that most of the
money had, in fact, arrived.

Added stage 3b (`core.match._stage3b_short_pay`): after stage 3 fails,
look for a single unambiguous credit within the window whose amount is
at least half of gross but less than gross and doesn't exactly match
any hypothesis. Book it as a genuine partial match - RECEIVED = what
arrived, SHORT = the shortfall, and correctly labelled `SHORT_PAID`
rather than lumped in with invoices that received nothing at all. The
half-of-gross floor exists so an unrelated small credit (a stray
personal transfer, a different client's payment) doesn't get
mistakenly absorbed as a "short pay" on an unrelated invoice.

Effect on the golden batch: `UNMATCHED_INVOICE` dropped from 11 to 9,
`SHORT_PAID` went from 0 (never fired, ever) to 3, and "rupees at risk"
rose from Rs 32,075.82 to Rs 39,314.72 - money that was always sitting
in the exception list, just filed under the wrong, less actionable
code. Auto-match rate barely moved (82.81% -> 81.25%), because a
short-paid invoice still correctly counts as "not fully matched" either
way - this fix is about the QUALITY of the exception classification,
not the raw match-rate number.

Found by re-reading the exception taxonomy against `core/match.py` line
by line and asking, for each of the 14 codes, "where does this actually
get assigned?" - three others (`SPLIT_PAYMENT`, `MERGED_PAYMENT`,
`TDS_NOT_IN_26AS`) checked out fine; `SHORT_PAID` didn't.

## Entry 7 — 2026-09-03 — building the 12-defect eval broke the ablation, which broke a bigger assumption

Building `data/holdout.py` (12 hand-planted, individually-labelled
defects, per plan/baaki.md §10) and `eval/defects.py` surfaced a chain
of four connected problems, in the order they were actually found.

**1. `PLATFORM_COMMISSION_VARIANCE` and `MERGED_PAYMENT` were also never
assigned** - the same class of gap as Entry 6's `SHORT_PAID`, just not
yet noticed because nothing in the golden batch exercised them.
`PLATFORM_COMMISSION` had no rate-table entry at all (it isn't a
statutory TDS rate, it's a privately contracted percentage -
`Client.contracted_commission_bps`), so `core.compose` silently
returned only the `no_deduction` hypothesis for it. Added a dedicated
branch in `hypotheses_for_invoice` for `DeductionKind.PLATFORM_COMMISSION`
that reads the contracted rate directly, plus a guard in
`core.match._book_matched_invoice` so non-TDS deductions never get
checked against Form 26AS (they have no 26AS concept at all - an
earlier pass would have wrongly flagged every platform-commission
invoice as `TDS_NOT_IN_26AS`). `MERGED_PAYMENT` was booked correctly
money-wise via `_stage4_merge` but the exception itself was never
raised - added it alongside the existing `SPLIT_PAYMENT` emission.

**2. `_stage4_split`/`_stage4_merge` search the ENTIRE credit pool,
unscoped by client.** First real run of `eval/defects.py` returned a
"split payment" for HOLD-08 built from three credits belonging to
*three different, unrelated clients* that happened to sum to the
target amount - the subset-sum search has no idea which credits
plausibly belong to the invoice's own counterparty. Added the same
narration-substring scoping used elsewhere in the matcher to both
functions - the target amount alone is not a strong enough signal once
a batch has enough credits in it (a birthday-paradox problem for
exact-sum matching).

**3. Fixing #2 exposed that `_stage4_merge` could never fire for
undeducted invoices at all** - it only ever searched for a hypothesis
labelled `"lawful_correct"`, but a `DeductionKind.NONE` invoice's only
hypothesis is labelled `"no_deduction"`. Generalised the target-hypothesis
lookup to try both.

**4. The deepest one: fixing stage 3's tie-break with a free,
deterministic narration-substring check (added so ties aren't always
resolved by an arbitrary "whichever credit came first" pick) accidentally
solved the original ablation scenario (`INV-TIE-A`/`INV-TIE-B`) for
free, with no LLM involved at all.** Those two credits' narrations
spelled the counterparty name out in full
("...ARJUNTEXTILES..."-style concatenation), so a plain substring check
already resolves the tie - meaning the "ablation" was never actually
testing what an LLM adds; it was testing whether anyone remembered to
check the name at all. Caught this by rerunning the full test suite
after the stage-3 change and watching the *ablation's own* pinned-down
test fail with the CORRECT outcome instead of the expected WRONG one -
worth reading twice, because a passing-for-the-wrong-reason test would
have been much easier to miss than a failing one.

Fixed by keeping `INV-TIE-A`/`INV-TIE-B` as a (now-passing) regression
test for the free substring tier, and adding a second pair,
`INV-TIE-C`/`INV-TIE-D`, whose narrations abbreviate the counterparty
name the way real bank narrations often do under a character budget
("BLUPEAK CNSLTNG" for "BluePeak Consulting") - close enough for a
human or a language model to recognise, but genuinely not a substring
of the full name. `eval/ablation.py` now measures the C/D pair.

**And while re-testing that:** the *original* tier-3 fallback for a
truly unresolved tie - "just pick whichever credit was found first" -
turned out to be the same root cause as problem #2, applied to stage 3
directly. Holdout defect 6 (`HOLD-06`, a short payment for "Quillon
Media") was silently matching a completely unrelated credit belonging
to "Casterbridge Textiles" purely because both had the same exact
predicted amount and neither the client nor the narration were checked
before falling back to "first found." Changed the rule: if every tied
candidate's narration carries *some* readable text and NONE of it names
this client, that's strong evidence the match is a coincidence, not
real ambiguity - the matcher now declines outright (falls through to
`UNMATCHED_INVOICE`) rather than guessing. This is the same "never
commit above threshold without real confidence" principle the project
already applies to the LLM layer, just applied to the deterministic
layer's own tie-break too. It also reframed `INV-TIE-C`/`D`'s "off"
outcome: without narration understanding, the matcher doesn't
confidently swap the two payments anymore - it honestly admits it can't
tell them apart. That is arguably a *better* story for the ablation
than a wrong answer would have been: the deterministic core is safe by
default, and the LLM's job is to convert an honest "I don't know" into
a correct "I know" - not to prevent a wrong guess that a safer default
already prevents.

Net effect on the golden batch: auto-match rate moved from 81.25% to
75.76% - a real, expected drop, not a regression. Declining more
often instead of guessing more often is the entire point; a lower
number that can be trusted beats a higher one that occasionally can't.
The 12-defect holdout eval, once all of the above landed, found 11/12
defects correctly classified, with the 12th (`FX_SPREAD_UNEXPLAINED`)
an honestly documented known gap - see `eval/defects.py` output and
`data/holdout.py`'s `PlantedDefect.known_gap` field.

## Entry 8 — 2026-09-05 — the other two missing exception codes: OVER_PAID and GATEWAY_FEE_VARIANCE

A post-submission audit (re-checking, for every one of the 15
`ExceptionCode` members, "where does this actually get assigned?" - the
same method that caught `SHORT_PAID` in Entry 6) turned up two more:
`OVER_PAID` and `GATEWAY_FEE_VARIANCE` were both defined with action
strings in `core/classify.py` and never assigned anywhere in
`core/match.py`. Unlike `FX_SPREAD_UNEXPLAINED` (an honestly documented
known gap - no FX hypothesis exists at all), these two had no excuse:
the machinery to support them was already sitting right next to them.

**`GATEWAY_FEE_VARIANCE`** turned out to be a copy of the
`PLATFORM_COMMISSION` fix from Entry 7 - `DeductionKind.GATEWAY_FEE`
had an enum value and nothing else. Added a `Client.contracted_mdr_bps`
field (Razorpay's MDR is set by the merchant's own rate card, not a
statutory table, same as platform commission) and a `GATEWAY_FEE`
branch in `core/compose.py` that models the lawful fee AND its GST
component together - `plan/baaki.md` §7 is explicit that Razorpay's MDR
carries "18% GST on the fee itself," so the deduction is
`mdr + 18% of mdr`, not just `mdr`. A charge above the contracted rate
card raises `GATEWAY_FEE_VARIANCE`. `core/match.py`'s existing
`is_statutory_tds` guard (from Entry 7) already routes non-TDS
deductions away from the Form 26AS check, so no match.py change was
needed there - only the new hypothesis branch and the exception
mechanism already wired to `hyp.exception_if_matched`.

**`OVER_PAID`** needed genuinely new matcher logic, not a copy of an
existing pattern - it's the mirror image of Entry 6's `SHORT_PAID`
stage, for a credit that's LARGER than any hypothesis predicts rather
than smaller. Added `core.match._stage3c_over_paid`, run last (after
split/merge and short-pay have had their chance, for the same reason
short-pay is ordered last: a genuine multi-part settlement must never
get mistaken for a simple over/under-payment on one of its parts).
Scoped to a single narration-confirmed candidate within
`OVER_PAY_MAX_FRACTION` (1.5x) of gross - the same "don't guess without
evidence" discipline as everywhere else in this matcher. The booking
function, `_book_over_paid`, keeps the invoice's own ledger bucket
accounting exact (`RECEIVED = gross`, nothing else) and records the
excess as an exception tied to both the invoice and the credit, rather
than inventing a fifth bucket - the excess isn't part of what was
invoiced, so it doesn't belong in the per-invoice conservation
identity.

Extended `data/holdout.py` with two new planted defects (13:
`OVER_PAID`, 14: `GATEWAY_FEE_VARIANCE`) rather than the golden batch,
following the precedent Entry 7 already set for `PLATFORM_COMMISSION`
and `MERGED_PAYMENT` - these are rare, deliberately-triggered cases
better demonstrated as named, checkable ground truth than folded into
the random batch's anomaly cycle. Both were caught correctly on first
run, with zero false positives elsewhere in the batch. The 14-defect
eval now finds 13/14, with `FX_SPREAD_UNEXPLAINED` the sole remaining
honest gap.

No change to the golden batch's own numbers (auto-match rate, rupees
at risk) - neither `PLATFORM_COMMISSION` nor `GATEWAY_FEE` nor
`OVER_PAID` are exercised by the random generator, only by the holdout
batch, so this was a pure coverage fix with zero risk to the numbers
already reported in the README and, by extension, in the demo video
script.

## Entry 9 — 2026-09-05 — the tax rates were finally checked, and one of them was wrong

Every rate in `core/rules/registry.py` had carried a
`verification_status = "UNVERIFIED - confirm against IT Dept before
production use"` field since the day it was written - this was the
single highest-severity open risk in the whole project by my own
earlier assessment, and it stayed open the longest because it needed
research, not code. Went through all nine rows in plan/baaki.md §7 one
at a time, searching multiple independent sources per row and
specifically looking for the exact number and effective date, not just
the general topic (a search for "is 194J still 30,000" surfaces plenty
of stale content confidently repeating the pre-Budget-2025 figure).

**Two real problems, one of which was a live bug:**

1. **The no-PAN override rate was wrong for one specific case.**
   `NO_PAN_OVERRIDE_BPS` (20%) was applied uniformly to every deduction
   kind when `pan_on_file=False`. Turns out s.206AA carries a specific
   proviso, inserted by the Finance Act 2019 and effective 1 April
   2020, that substitutes 5% in place of the generic 20% specifically
   for payments under 194-O. A 194-O deduction computed without PAN was
   overstating the deduction by 4x. Fixed with a dedicated
   `NO_PAN_OVERRIDE_194O_BPS = 500` and a branch in `RuleSet.rule_for`
   that checks the deduction kind before applying the override - the
   same kind of "the exception to the rule is itself a rule" trap that
   `TDS_ON_GST_INCLUSIVE` already exists to catch on the GST side.

2. **The 194-O threshold description was true but incomplete.** The
   ₹5,00,000 threshold only applies to individual/HUF participants who
   furnish PAN/Aadhaar - a non-individual payee (a registered company
   or LLP) has no threshold at all, TDS applies from the first rupee.
   `Client` has no entity-type field, so the code has always applied
   the individual/HUF threshold unconditionally - which happens to be
   correct for this project's actual persona (a freelancer/sole
   earner), but was an unstated assumption rather than a documented
   scope decision. Fixed by documenting it explicitly in
   `core/rules/registry.py` and in the plan; did not add an
   entity-type field, since every other client in this project's data
   model is implicitly an individual/HUF anyway and a real fix would
   need a genuine feature (entity type on `Client`) with no batch data
   to exercise it.

**One claim that could easily have been a hallucination and wasn't:**
the plan's boldest, least-obvious claim - that TDS provisions get
recited under a brand-new "Section 393" starting 1 April 2026 - is the
kind of specific, checkable-but-easy-to-fabricate detail that would be
genuinely embarrassing to get wrong in front of a payments-literate
reviewer. Checked it against three independent sources describing the
same mechanism (a single umbrella section organised into six numbered
tables, with a worked example translating "194C at 1%" into
"s.393(1), Table Sl. No. 6(i), payment code 1017") - it's real. While
checking it, found a related fact the original research had missed:
s.206AA itself (the no-PAN override) is *also* being restructured,
merging with s.206CC into a new s.397(2). Left unfixed - it only
affects which citation string gets displayed for the no-PAN case,
never the rate - but noted in the plan as a residual gap rather than
silently ignored.

**What I could not do:** incometaxindia.gov.in's own FAQ pages return
403 on automated fetches, so true primary-source confirmation wasn't
achievable for the statutory rows in the time available - every row
above is corroborated by 2-3 independent secondary sources agreeing on
the same number, effective date, and amending Act, which is
meaningfully stronger evidence than the single-source cross-check this
project shipped with originally, but it is still not the primary
document itself. Said so plainly in plan/baaki.md rather than
overstating the confidence level.

## Entry 10 — 2026-09-05 — switched the LLM layer from Claude to Gemini

No `ANTHROPIC_API_KEY` was available in the environment this project
was built in, and a `GEMINI_API_KEY` was - so the LLM layer moved from
Anthropic's Claude API to Google's Gemini API (`google-genai` SDK)
rather than staying blocked on credentials nobody had. This only
touches `llm/client.py`, `llm/narration.py`, and `llm/narrative.py` -
`llm/verify.py` (the hallucination/binding/identity gate) never
imported the SDK at all, by design (see its own docstring), so the
safety mechanism this project actually depends on didn't move an inch.

The mapping was mostly mechanical once the correct current SDK syntax
was confirmed (`client.models.generate_content(model=..., contents=...,
config=types.GenerateContentConfig(system_instruction=..., response_mime_type=
"application/json", response_schema=PydanticModel))`, with the parsed
result on `response.parsed` - the Gemini equivalent of Claude's
`client.messages.parse(..., output_format=Model)` / `.parsed_output`).
Two things did NOT carry over and were dropped rather than faked:
Claude's `thinking`/`effort` controls have no direct Gemini equivalent
in this code path, so the calls are now plain (no reasoning-effort
tuning) - a reasonable trade for two structured-extraction tasks that
were already using low/medium effort, not deep reasoning. Model choice
is `gemini-2.5-flash` for both jobs, a single constant in
`llm/client.py::MODEL`.

`tests/test_llm_narration_mocked.py`'s fake client changed shape to
match (`client.models.generate_content()` returning `.parsed`, not
`client.messages.parse()` returning `.parsed_output`) - the fake client
supplies the *response*, but `llm/narration.py` still constructs a
real `google.genai.types.GenerateContentConfig` object internally, so
these tests only pass if that import and construction actually work,
not just if the mock is self-consistent. `requirements.txt` swapped
`anthropic` for `google-genai`. All 48 tests pass unchanged in count -
this was a like-for-like provider swap, not new functionality.

**Update, same day, once a real key arrived:** it does. See Entry 11
for the full live run - both jobs work, and getting there surfaced
three more real things worth fixing.

## Entry 11 — 2026-09-05 — the first live Gemini calls, and three real things they broke

Got a real `GEMINI_API_KEY`. First instinct was to just flip the
switch and run `eval/ablation.py --live`. It surfaced three genuine
issues in order, each fixed as it appeared rather than worked around.

**1. Model choice, checked against the live API, not guessed.**
Asked to prefer a newer model than 2.5. Rather than guess a plausible-
sounding name, called `client.models.list()` against the real key -
`gemini-3.5-flash` through `gemini-3.8-flash` all genuinely exist.
Smoke-tested two candidates with the actual `response_schema` call this
project uses: `gemini-3.6-flash` returned a correct structured result;
`gemini-3.8-flash` returned a 503 ("currently experiencing high
demand" - expected for a just-released model at capacity). Picked
`gemini-3.6-flash`.

**2. Without a reference list, the model correctly declines to guess a
specific unlisted name - which is honest, but breaks the ablation.**
First real run of `parse_narration` against the garbled ablation
narration ("BLUPEAK CNSLTNG") came back as "Blupeak Cnsltng" - title-
cased, not expanded to "BluePeak Consulting". That's the right call
for a model given zero context about who the payer might be; the
mocked test had assumed the model would somehow guess the exact
intended expansion out of thin air, which was never a fair test.
`core.match`'s tier-2 tie-break does exact token-set overlap
(`_name_tokens("Blupeak Cnsltng")` shares nothing with
`_name_tokens("BluePeak Consulting")` - the letters themselves differ),
so this silently failed to resolve the tie even with a "working" LLM
call.

The fix is the same one a human does: check the narration against your
own client list. `parse_narration`/`parse_narration_verified` now take
an optional `known_counterparties` list, and the prompt asks the model
to return a listed name verbatim if the narration plausibly matches
one (even through an abbreviation or typo), and only falls back to its
own literal reading otherwise - with an explicit instruction not to
force a match on unrelated narrations. `eval/ablation.py` passes
`[c.name for c in batch.clients]`. Verified live, three cases: the
garbled "BluePeak" narration correctly resolved to the exact roster
string; the garbled "Fernhill" narration too; and a deliberately
unrelated narration ("RANDOMPERSON") was correctly NOT forced onto any
roster entry, coming back as a plain cleaned-up "Random Person"
instead - proving the fix adds a real capability without weakening the
"don't invent things" instruction.

**3. `gemini-3.6-flash`'s free tier caps at 20 `generate_content` calls
per day, per model - not a short rate limit, a hard daily wall.**
Running the ablation's original design (call the live parser on every
credit in the batch, ~70+ of them) hit a 429 immediately. The retry
logic added for (expected, from finding #1) transient 503s does not
help here - exponential backoff cannot make a daily quota reset
sooner. Two real fixes, not one workaround:

- `llm/client.py::call_with_retry` now distinguishes the two failure
  modes properly: a 503 gets retried with backoff on the *same* model
  (a genuine "try again in a moment" case); a 429 immediately falls
  back to the next model in `FALLBACK_MODELS` instead (Google scopes
  the free quota per-model-per-project, so a different model has its
  own untouched bucket - retrying the same exhausted model would just
  burn time to fail the same way again). The fallback list itself was
  built by testing candidates against the live key rather than
  guessing: the entire `gemini-2.5-*` line returned 404 "no longer
  available to new users" for this specific key/project, which a
  guess would never have caught - `gemini-3.5-flash-lite`,
  `gemini-3.1-flash-lite`, and `gemini-flash-latest` all confirmed
  working.
- `eval/ablation.py::_build_hint_live` no longer calls the LLM on
  every credit in the batch regardless of whether it needs it. It now
  runs a no-hint baseline pass first, collects the credit IDs still
  sitting in `UNMATCHED_CREDIT` (10 out of 73 on the golden batch, the
  two ablation-relevant credits among them), and only escalates those
  to the live parser. This is not a workaround for the quota - it is
  the correct design regardless of quota: there is no reason to spend
  an API call re-examining a narration whose invoice a cheap
  deterministic stage already matched correctly. The "right tool in
  the right place" principle this project already applies to its own
  tie-break tiers, now applied to when the tool gets called at all.

**The actual live proof, once all three landed:**

```
=== OFF: no narration hint at all ===
  INV-TIE-C -> None   INV-TIE-D -> None   (declined, not guessed)

=== ON: real Gemini narration parser ===
  (fell back to gemini-3.5-flash-lite once gemini-3.6-flash's daily quota was hit)
  live parser called for 10/73 credits - only those left unresolved
  discard rate: 0.0% over 10 credits
  auto-match rate: 75.76% -> 78.79%
  INV-TIE-C -> CR-TIE-C   INV-TIE-D -> CR-TIE-D   resolved correctly? True
```

Also ran job 3 (`build_narrative`) live against the `INV-GHOST-01`
worked example: the model produced a correctly-worded chase message
citing the exact injected Rs 2,000.00 figure, with no invented numbers
and a professional, first-person tone - matching the design intent in
`llm/narrative.py`'s own docstring on the first real attempt.

Net effect: both LLM jobs are now proven against the real API, not
just mocks. Zero regressions - all 48 tests still pass, the
deterministic golden-batch numbers (auto-match 75.76%, Rs 36,575.82 at
risk) are completely unaffected, since none of this touched `core/`.

## Entry 12 — 2026-09-05 — real Razorpay test-mode calls, and Smart Collect is a dashboard toggle away

Got real Razorpay test-mode credentials. Wrote
`data/fetch_razorpay_fixtures.py` to make the ~15 schema-fidelity calls
the plan always wanted, reading `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`
from the environment only - the script refuses to run at all if the
key doesn't start with `rzp_test_`, and checks every saved response
for the literal secret string before writing it to disk, on top of
`.gitignore` already excluding the credentials file itself.

**Two real bugs in the fixture script, found by actually running it:**

1. The Invoices API's own documentation examples don't use the `draft`
   parameter at all, and passing `"draft": 1` (the value the docs
   *do* mention elsewhere) got rejected outright: `"The selected draft
   is invalid"`. Turns out invoices on this account issue immediately
   on creation regardless - the fix was to stop fighting that and
   drop the redundant separate `/issue` call, which was 400ing anyway
   with "Operation not allowed for Invoice in issued status" once
   creation had already issued it.
2. Running the script twice back-to-back failed customer creation with
   "Customer already exists for the merchant" - Razorpay dedupes
   customers by contact/email. Fixed by suffixing both with a
   timestamp tag so the script is safely re-runnable.

**One genuine platform limitation, not a bug:** creating a UPI payment
link returns `"UPI Payment Links is not supported in Test Mode. Please
experience the product in Live Mode."` - saved as-is; it's real,
documented behaviour, not something to route around.

**The one real blocker: Smart Collect isn't enabled on this account.**
`POST /v1/virtual_accounts` returns `"The requested URL was not found
on the server"` - Razorpay's generic error for a product that isn't
turned on for a given merchant, not a routing problem (the URL matches
the official docs exactly, and a second attempt with the docs' own
verbatim example payload gives the identical error). This needs a
dashboard-side product enablement only the account owner can do -
nothing in this codebase can toggle it. **12 of 15 planned calls
succeeded outright** (customers, invoices - both creation paths,
payment links, orders); the other 3 are the UPI-links limitation plus
the Smart Collect create/fetch pair, both documented rather than
hidden. All 15 responses are real and saved to
`data/fixtures/razorpay_raw/`.

**Built the Smart Collect A/B anyway** (`eval/smart_collect_ab.py`) -
the actual measured evidence for "Razorpay is essential, not
decorative" that was, until today, a paragraph of prose with nothing
behind it. Generates the identical set of invoices twice: Run A as
bank-statement-style credits with no payer name in the narration at
all (a realistic case - NEFT/RTGS lines and CSV exports commonly carry
no free-text name, only a UPI app notification sometimes does); Run B
as if collected through a Smart Collect identifier, so every credit
already names its own client via `Credit.razorpay_customer_identifier`
(a field `core/models.py` and `core/match.py`'s stage-1 identity check
already supported - no core code changed for this). Every 4th invoice
deliberately shares its amount with the invoice before it, for a
different client, a few days apart - the realistic case of two clients
paying similar round-ish sums in the same week.

First attempt at this came back 100%/100% - no gap at all. Wrong
result, and worth explaining why: the "anonymous" Run A narrations
still spelled the client's name out in full text
("UPI/CR/.../BLUEPEAKCONSULTING/HDFC"), which `core.match`'s own free
tier-1 substring check (added in Entry 7) resolves for free - the
exact same trap Entry 7 already fell into once with the LLM ablation's
first design. Fixed the same way: made Run A's narrations genuinely
anonymous (rail + reference number only, no name field), matching a
plain bank-statement export rather than a UPI app notification that
happens to show a name. Result with that corrected:

```
Run A - anonymous UPI credits (40 invoices, 8 clients):
  auto-match rate: 50.00%

Run B - Razorpay Smart Collect identifiers (40 invoices, 8 clients):
  auto-match rate: 100.00%
  resolved via certain stage-1 identity: 40/40

delta: +50.00 percentage points
```

Checked this isn't a fluke: 10 of 40 invoices carry a deliberately
collided amount, and each collision touches two invoices (the original
and its copy) - 20/40 = 50%, exactly matching the observed gap.
Deterministic across five separate process runs.
`tests/test_smart_collect_ab.py` pins four things down: both runs
conserve, Smart Collect is never worse than anonymous (it can only add
information, never remove it), every Run B invoice resolves via
certain stage-1 identity (not a guess), and Run A's collision injection
is actually firing (a floor of 90% would mean the test data itself
stopped testing anything).

**Honesty note on what's real and what's modelled:** the 15 fixtures
in `data/fixtures/razorpay_raw/` are genuine live API responses. The
Smart Collect identifier format in `eval/smart_collect_ab.py`
(`"va_<15 hex chars>"`, matching Razorpay's real virtual-account ID
prefix) is modelled on the officially documented request/response
schema, not a live response, because generating a real one needs the
dashboard toggle above. The matching *logic* the A/B measures
(`core.match`'s stage-1 identity resolution) is real, already tested,
and already proven live on the golden batch - only the identifier
strings themselves are synthetic here, and the script's own docstring
says so explicitly rather than letting a reader assume otherwise.
