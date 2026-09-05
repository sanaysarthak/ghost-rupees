"""
The staged matcher. See plan/baaki.md §5.

Design note on why the ledger can never show a nonzero residual once
this runs: every invoice's gross amount is placed into RECEIVED /
DEDUCTED_CREDITABLE / DEDUCTED_UNCREDITABLE / SHORT and those four
always sum to gross by construction - an unmatched invoice simply
puts its whole gross into SHORT. Gate 1 (assert_conserves) is
therefore a check that this code never double-books or drops a
paisa, not a check of match *quality*. Match quality is the
auto-match rate (Ledger.auto_match_rate_pct) - the percentage of
invoices whose SHORT bucket is zero - and that is a real, gameable
number this module has to earn.

Stage 2 (UTR lookup via Razorpay's "Fetch Payments Using UTR" API) and
stage 5 (LLM tie-break) are implemented as clearly-labelled seams:
stage 2 takes an injectable resolver that is a no-op by default
(wiring it to the live API is a one-function job, deliberately left
for when real Razorpay credentials are available); stage 5 auto-picks
by a documented priority order and flags the decision as `ambiguous`
rather than silently guessing.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from core.classify import Exception_, ExceptionCode
from core.compose import Hypothesis, gross_amount, hypotheses_for_invoice
from core.ledger import Bucket, Ledger, LedgerLine
from core.models import Batch, Credit, Form26ASEntry, Invoice
from core.money import Paisa
from core.proof import Proof
from core.rules.registry import resolve as resolve_ruleset

MATCH_WINDOW_DAYS = 45
MAX_SET_SIZE = 3

# Priority order for auto-resolving a tie between hypotheses that predict
# the identical net (stage 5's deterministic fallback before/without an
# LLM tie-break call).
_HYPOTHESIS_PRIORITY = [
    "lawful_correct",
    "no_deduction",
]


def _utr_normalise(u: str | None) -> str | None:
    if not u:
        return None
    return re.sub(r"[^A-Za-z0-9]", "", u).upper()


def _prior_base_paisa_this_fy(batch: Batch, client_id: str, fy, before_date: date) -> Paisa:
    """
    TDS thresholds apply cumulatively per payee per financial year, not
    per invoice (see core.compose.hypotheses_for_invoice). Sums this
    client's OTHER invoices in the same FY that were issued strictly
    before `before_date`.
    """
    total = 0
    for other in batch.invoices:
        if other.client_id != client_id:
            continue
        if other.issue_date >= before_date:
            continue
        if not fy.contains(other.issue_date):
            continue
        total += int(other.service_amount_paisa)
    return Paisa(total)


@dataclass
class MatchOutcome:
    invoice_id: str
    matched: bool
    stage: str | None
    credit_ids: list[str]
    hypothesis: Hypothesis | None
    ambiguous: bool = False
    alternate_labels: list[str] = field(default_factory=list)


def _stage1_identity(invoice: Invoice, pool: list[Credit]) -> Credit | None:
    for c in pool:
        if c.razorpay_payment_id and c.razorpay_payment_id in invoice.notes:
            return c
        if c.razorpay_customer_identifier and c.razorpay_customer_identifier in invoice.notes:
            return c
    return None


def _stage2_utr(invoice: Invoice, pool: list[Credit],
                 utr_resolver: Callable[[str], str | None]) -> Credit | None:
    """
    utr_resolver maps a normalised UTR -> a razorpay_payment_id, standing in
    for a call to Razorpay's Fetch-Payments-Using-UTR API. Default resolver
    (see resolve_all) always returns None, so this stage is a documented
    no-op until wired to real credentials.
    """
    for c in pool:
        norm = _utr_normalise(c.utr)
        if not norm:
            continue
        resolved_payment_id = utr_resolver(norm)
        if resolved_payment_id and resolved_payment_id in invoice.notes:
            return c
    return None


def _within_window(invoice: Invoice, credit_date: date) -> bool:
    return invoice.issue_date <= credit_date <= (invoice.due_date + timedelta(days=MATCH_WINDOW_DAYS))


def _name_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", s.lower())) - {"the", "and", "co", "llp", "pvt", "ltd"}


def _stage3_hypothesis(
    invoice: Invoice, hyps: list[Hypothesis], pool: list[Credit], client_name: str = "",
    narration_hint: dict[str, tuple[str | None, str | None]] | None = None,
) -> tuple[Credit, Hypothesis, bool, list[str]] | None:
    """
    Cross-credit ties (two or more DISTINCT credits predicting the same
    net for this invoice - a genuine cross-client collision, not just
    two hypotheses on the same credit) are resolved in three escalating
    tiers, cheapest first - this is the project's "right tool in the
    right place" line made literal:

    1. Deterministic: does a candidate's raw narration directly contain
       this client's (compacted, letters-only) name as a substring? No
       model call, free, and resolves the common case where the bank
       narration spells the name out in full (however it's punctuated).
    2. narration_hint: an LLM-parsed counterparty name (from
       llm.narration, passed in by the CALLER as plain tuples - never
       imported directly here, see the module docstring and
       tests/test_import_boundary.py) for cases where tier 1 fails
       because the narration abbreviates, truncates, or garbles the
       name in a way substring matching can't see through but language
       understanding can.
    3. Arbitrary first-found - the residual, explicitly-flagged
       `ambiguous=True` case neither tier could resolve.
    """
    candidates = [c for c in pool if _within_window(invoice, c.value_date)]
    matches: list[tuple[Credit, Hypothesis]] = []
    for c in candidates:
        for h in hyps:
            if int(c.amount_paisa) == int(h.predicted_net_paisa):
                matches.append((c, h))
    if not matches:
        return None
    if len(matches) == 1:
        c, h = matches[0]
        return c, h, False, []

    by_credit: dict[str, list[Hypothesis]] = {}
    for c, h in matches:
        by_credit.setdefault(c.credit_id, []).append(h)
    distinct_credit_ids = list(by_credit.keys())

    chosen_credit_id: str | None = None
    resolved_by_narration = False
    checked_and_zero_matched = False

    if len(distinct_credit_ids) > 1 and client_name:
        compact_client = _compact(client_name)
        # tier 1: deterministic substring match against raw narration
        substring_matched = [
            cid for cid in distinct_credit_ids
            if compact_client and compact_client in _compact(next(c for c, _ in matches if c.credit_id == cid).raw_narration)
        ]
        if len(substring_matched) == 1:
            chosen_credit_id = substring_matched[0]
            resolved_by_narration = True
        elif narration_hint:
            # tier 2: LLM-parsed counterparty name
            client_tokens = _name_tokens(client_name)
            hint_matched = [
                cid for cid in distinct_credit_ids
                if narration_hint.get(cid) and narration_hint[cid][0]
                and _name_tokens(narration_hint[cid][0]) & client_tokens
            ]
            if len(hint_matched) == 1:
                chosen_credit_id = hint_matched[0]
                resolved_by_narration = True

        if chosen_credit_id is None and not substring_matched:
            # Every tied candidate's narration carries SOME identifiable
            # text (checked below) and NONE of it names this client - that
            # is strong evidence the "match" is a pure amount coincidence
            # between unrelated clients, not real uncertainty about which
            # of several plausible candidates is right. Declining here
            # (returning no match at all, rather than guessing tier 3)
            # is the same "never commit above threshold without real
            # confidence" principle this project applies everywhere else -
            # an honest miss beats a silent wrong match. Found empirically
            # via the 12-defect holdout eval: without this, an unrelated
            # invoice's own exact-amount hypothesis was silently stealing
            # another client's credit.
            any_readable_name = any(
                re.search(r"[a-z]{4,}", next(c for c, _ in matches if c.credit_id == cid).raw_narration.lower())
                for cid in distinct_credit_ids
            )
            if any_readable_name:
                checked_and_zero_matched = True

    if checked_and_zero_matched:
        return None

    if chosen_credit_id is None:
        # tier 3: arbitrary first-found credit - the residual gap where
        # narrations carried no identifiable name at all to check against.
        chosen_credit_id = distinct_credit_ids[0]

    chosen_credit = next(c for c, _ in matches if c.credit_id == chosen_credit_id)
    hyp_options = by_credit[chosen_credit_id]

    def priority(h: Hypothesis) -> int:
        try:
            return _HYPOTHESIS_PRIORITY.index(h.label)
        except ValueError:
            return len(_HYPOTHESIS_PRIORITY) + 1

    hyp_options_sorted = sorted(hyp_options, key=priority)
    chosen_hyp = hyp_options_sorted[0]
    alternates = [h.label for h in hyp_options_sorted[1:]]
    # only flag as ambiguous if a *cross-credit* tie remains unresolved -
    # ties purely between hypotheses on the SAME credit are a modelling
    # nicety (which lawful story explains this one credit), not a real
    # "which money is this" ambiguity, so they don't need to be flagged
    # to the same degree.
    ambiguous = len(distinct_credit_ids) > 1 and not resolved_by_narration
    return chosen_credit, chosen_hyp, ambiguous, alternates


SHORT_PAY_MIN_FRACTION = 0.5   # a candidate short-pay credit must be at least
                                # this fraction of gross, or it's more likely
                                # an unrelated small credit than a short-pay


def _stage3b_short_pay(invoice: Invoice, hyps: list[Hypothesis],
                        pool: list[Credit], client_name: str = "") -> Credit | None:
    """
    Not every gap has a lawful explanation. A client can simply pay less
    than invoiced with no deduction basis at all. Stage 3 (exact
    hypothesis match) already failed by the time this runs, so any
    candidate here is, by construction, an amount that matches NO lawful
    or common-error hypothesis - the defining feature of a short payment.
    Requires a single unambiguous candidate within the window, no less
    than SHORT_PAY_MIN_FRACTION of gross, AND (when a client name is
    given) whose narration shares a name token with this invoice's
    client - a bare amount-range filter alone can accidentally scoop up
    a credit that actually belongs to a different client's invoice
    (their own exact match just hasn't been tried yet in the processing
    order), which is a worse error than declining to guess.
    """
    gross = int(gross_amount(invoice))
    floor = int(gross * SHORT_PAY_MIN_FRACTION)
    # Narrations concatenate the counterparty name with no spaces (e.g.
    # "BLUEPEAKCONSULTING"), so a token-SET overlap against the spaced
    # client name never matches - compare compact (letters-only) forms
    # with substring containment instead.
    compact_client = re.sub(r"[^a-z]", "", client_name.lower()) if client_name else ""
    candidates = [
        c for c in pool
        if _within_window(invoice, c.value_date) and floor <= int(c.amount_paisa) < gross
        and (not compact_client or compact_client in re.sub(r"[^a-z]", "", c.raw_narration.lower()))
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _compact(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


OVER_PAY_MAX_FRACTION = 1.5   # a candidate over-payment credit must be no
                               # more than this multiple of gross, or it's
                               # more likely an unrelated larger credit
                               # (or a genuine split/merge case) than a
                               # simple over-payment on this one invoice


def _stage3c_over_paid(invoice: Invoice, pool: list[Credit], client_name: str = "") -> Credit | None:
    """
    The mirror image of _stage3b_short_pay: a customer can pay MORE than
    invoiced (a duplicate transfer, a rounding-up, an advance folded in)
    with no hypothesis predicting that exact amount. By the time this
    runs, stages 1-3 and stage 4 (split/merge) have already had their
    chance, so a credit landing here genuinely doesn't fit any of those
    shapes. Same discipline as the short-pay stage: scoped to a single,
    narration-confirmed candidate within a bounded multiple of gross, so
    an unrelated larger credit doesn't get silently annexed onto this
    invoice.
    """
    gross = int(gross_amount(invoice))
    ceiling = int(gross * OVER_PAY_MAX_FRACTION)
    compact_client = _compact(client_name) if client_name else ""
    candidates = [
        c for c in pool
        if _within_window(invoice, c.value_date) and gross < int(c.amount_paisa) <= ceiling
        and (not compact_client or compact_client in _compact(c.raw_narration))
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _stage4_split(invoice: Invoice, hyps: list[Hypothesis],
                   pool: list[Credit], client_name: str = "") -> tuple[list[Credit], Hypothesis] | None:
    """
    One invoice settled across up to MAX_SET_SIZE credits. Scoped to
    credits whose narration names this client - unscoped subset-sum over
    an entire multi-client credit pool WILL eventually find a spurious
    combination that happens to sum correctly (confirmed empirically on
    the 12-defect holdout batch: it found a 3-credit "match" built from
    three different clients' unrelated payments).
    """
    compact_client = _compact(client_name) if client_name else ""
    candidates = [
        c for c in pool
        if _within_window(invoice, c.value_date)
        and (not compact_client or compact_client in _compact(c.raw_narration))
    ]
    for size in range(2, MAX_SET_SIZE + 1):
        for combo in itertools.combinations(candidates, size):
            total = sum(int(c.amount_paisa) for c in combo)
            for h in hyps:
                if total == int(h.predicted_net_paisa):
                    return list(combo), h
    return None


def _stage4_merge(invoice_group: list[Invoice], batch: Batch,
                   pool: list[Credit], client_name: str = "") -> tuple[Credit, dict[str, Hypothesis]] | None:
    """One credit covering up to MAX_SET_SIZE invoices for the same client.
    Same narration-scoping rationale as _stage4_split."""
    compact_client = _compact(client_name) if client_name else ""
    scoped_pool = [
        c for c in pool
        if not compact_client or compact_client in _compact(c.raw_narration)
    ]
    for size in range(2, MAX_SET_SIZE + 1):
        for combo in itertools.combinations(invoice_group, size):
            per_invoice_hyps = {}
            for inv in combo:
                inv_ruleset = resolve_ruleset(inv.issue_date)
                prior = _prior_base_paisa_this_fy(batch, inv.client_id, inv_ruleset.fy, inv.issue_date)
                per_invoice_hyps[inv.invoice_id] = hypotheses_for_invoice(
                    inv, inv_ruleset, batch.client(inv.client_id), prior_base_paisa_this_fy=prior
                )
            chosen = {}
            total = 0
            ok = True
            for inv in combo:
                # prefer "lawful_correct" (a statutory/contracted deduction
                # applies); fall back to "no_deduction" for invoices with no
                # deduction_kind at all - both are lawful, deterministic,
                # single-valued hypotheses, so either is a safe merge target.
                target = next(
                    (h for h in per_invoice_hyps[inv.invoice_id] if h.label == "lawful_correct"),
                    next((h for h in per_invoice_hyps[inv.invoice_id] if h.label == "no_deduction"), None),
                )
                if target is None:
                    ok = False
                    break
                chosen[inv.invoice_id] = target
                total += int(target.predicted_net_paisa)
            if not ok:
                continue
            for c in scoped_pool:
                if any(_within_window(inv, c.value_date) for inv in combo) and int(c.amount_paisa) == total:
                    return c, chosen
    return None


def _form26as_has(batch: Batch, tan: str | None, amount: Paisa, around: date) -> Form26ASEntry | None:
    if not tan:
        return None
    for e in batch.form26as:
        if e.deductor_tan == tan and int(e.amount_paisa) == int(amount):
            return e
    return None


def run_matcher(batch: Batch, *, utr_resolver: Callable[[str], str | None] | None = None,
                 narration_hint: dict[str, tuple[str | None, str | None]] | None = None) -> Ledger:
    utr_resolver = utr_resolver or (lambda _utr: None)
    ledger = Ledger()

    # --- pre-pass: duplicate UTR detection, remove duplicates from the pool
    seen_utr: dict[str, Credit] = {}
    duplicate_ids: set[str] = set()
    for c in batch.credits:
        norm = _utr_normalise(c.utr)
        if not norm:
            continue
        if norm in seen_utr:
            duplicate_ids.add(c.credit_id)
        else:
            seen_utr[norm] = c
    for c in batch.credits:
        if c.credit_id in duplicate_ids:
            ledger.add_credit_exception(Exception_.make(
                ExceptionCode.DUPLICATE_CREDIT,
                invoice_id=None, credit_id=c.credit_id, amount_paisa=c.amount_paisa,
                explanation=f"Credit {c.credit_id} shares UTR {c.utr!r} with an earlier credit.",
            ))

    pool: list[Credit] = [c for c in batch.credits if c.credit_id not in duplicate_ids]
    consumed: set[str] = set()

    unresolved: list[Invoice] = []

    for inv in batch.invoices:
        client = batch.client(inv.client_id)
        ruleset = resolve_ruleset(inv.issue_date)
        prior = _prior_base_paisa_this_fy(batch, inv.client_id, ruleset.fy, inv.issue_date)
        hyps = hypotheses_for_invoice(inv, ruleset, client, prior_base_paisa_this_fy=prior)
        available = [c for c in pool if c.credit_id not in consumed]

        credit = _stage1_identity(inv, available)
        stage = "stage1_identity"
        hyp: Hypothesis | None = None
        ambiguous = False
        alternates: list[str] = []

        if credit is not None:
            # identity match found the credit; still need the hypothesis
            # that explains its amount, so the deduction split is correct.
            hyp = next((h for h in hyps if int(h.predicted_net_paisa) == int(credit.amount_paisa)), None)
            if hyp is None:
                # identity is certain but amount doesn't match any modelled
                # hypothesis - fall back to "no_deduction" bookkeeping and
                # let the residual show up honestly as a variance.
                hyp = hyps[0]
        else:
            credit = _stage2_utr(inv, available, utr_resolver)
            stage = "stage2_utr"
            if credit is not None:
                hyp = next((h for h in hyps if int(h.predicted_net_paisa) == int(credit.amount_paisa)), hyps[0])

        if credit is None:
            found = _stage3_hypothesis(inv, hyps, available, client.name, narration_hint)
            stage = "stage3_hypothesis"
            if found is not None:
                credit, hyp, ambiguous, alternates = found

        if credit is not None and hyp is not None:
            consumed.add(credit.credit_id)
            _book_matched_invoice(ledger, batch, inv, hyp, credit, stage, ambiguous, alternates)
        else:
            unresolved.append(inv)

    # --- stage 4: split (one invoice / many credits)
    still_unresolved: list[Invoice] = []
    for inv in unresolved:
        client = batch.client(inv.client_id)
        ruleset = resolve_ruleset(inv.issue_date)
        prior = _prior_base_paisa_this_fy(batch, inv.client_id, ruleset.fy, inv.issue_date)
        hyps = hypotheses_for_invoice(inv, ruleset, client, prior_base_paisa_this_fy=prior)
        available = [c for c in pool if c.credit_id not in consumed]
        found = _stage4_split(inv, hyps, available, client.name)
        if found is not None:
            credits, hyp = found
            for c in credits:
                consumed.add(c.credit_id)
            _book_matched_invoice(ledger, batch, inv, hyp, credits, "stage4_split", False, [])
        else:
            still_unresolved.append(inv)

    # --- stage 4: merge (one credit / many invoices), grouped per client
    final_unresolved: list[Invoice] = []
    by_client: dict[str, list[Invoice]] = {}
    for inv in still_unresolved:
        by_client.setdefault(inv.client_id, []).append(inv)

    merged_invoice_ids: set[str] = set()
    for client_id, invs in by_client.items():
        available = [c for c in pool if c.credit_id not in consumed]
        merge_client_name = batch.client(client_id).name
        found = _stage4_merge(invs, batch, available, merge_client_name)
        if found is not None:
            credit, per_invoice_hyp = found
            consumed.add(credit.credit_id)
            for inv in invs:
                if inv.invoice_id in per_invoice_hyp:
                    _book_matched_invoice(
                        ledger, batch, inv, per_invoice_hyp[inv.invoice_id], credit,
                        "stage4_merge", False, [],
                    )
                    merged_invoice_ids.add(inv.invoice_id)
            if len(per_invoice_hyp) > 1:
                for inv in invs:
                    if inv.invoice_id in per_invoice_hyp:
                        ledger.add_invoice_exception(Exception_.make(
                            ExceptionCode.MERGED_PAYMENT,
                            invoice_id=inv.invoice_id, credit_id=credit.credit_id,
                            amount_paisa=gross_amount(inv),
                            explanation=f"Invoice {inv.invoice_id} was settled together with "
                                        f"{len(per_invoice_hyp) - 1} other invoice(s) in a single credit "
                                        f"({credit.credit_id}).",
                        ))

    for inv in still_unresolved:
        if inv.invoice_id not in merged_invoice_ids:
            final_unresolved.append(inv)

    # --- stage 3b: short-pay, tried last (after split/merge have had their
    # chance) so a genuine multi-credit split is never mistaken for a
    # single-credit short payment on one of its parts.
    still_final: list[Invoice] = []
    for inv in final_unresolved:
        client = batch.client(inv.client_id)
        ruleset = resolve_ruleset(inv.issue_date)
        prior = _prior_base_paisa_this_fy(batch, inv.client_id, ruleset.fy, inv.issue_date)
        hyps = hypotheses_for_invoice(inv, ruleset, client, prior_base_paisa_this_fy=prior)
        available = [c for c in pool if c.credit_id not in consumed]
        short_credit = _stage3b_short_pay(inv, hyps, available, client.name)
        if short_credit is not None:
            consumed.add(short_credit.credit_id)
            _book_short_paid(ledger, inv, short_credit)
        else:
            still_final.append(inv)
    final_unresolved = still_final

    # --- stage 3c: over-pay, tried after short-pay for the same reason -
    # give split/merge/short-pay every chance first, so a genuinely
    # multi-part settlement is never mistaken for a simple over-payment.
    still_final2: list[Invoice] = []
    for inv in final_unresolved:
        client = batch.client(inv.client_id)
        available = [c for c in pool if c.credit_id not in consumed]
        over_credit = _stage3c_over_paid(inv, available, client.name)
        if over_credit is not None:
            consumed.add(over_credit.credit_id)
            _book_over_paid(ledger, inv, over_credit)
        else:
            still_final2.append(inv)
    final_unresolved = still_final2

    # --- everything still unresolved: SHORT, full gross, UNMATCHED_INVOICE
    for inv in final_unresolved:
        gross = gross_amount(inv)
        ledger.add(LedgerLine(
            invoice_id=inv.invoice_id, bucket=Bucket.SHORT, amount_paisa=gross,
            note="No matching credit found within the match window.",
        ))
        ledger.add_invoice_exception(Exception_.make(
            ExceptionCode.UNMATCHED_INVOICE,
            invoice_id=inv.invoice_id, credit_id=None, amount_paisa=gross,
            explanation=f"Invoice {inv.invoice_id} has no matching credit within "
                        f"{MATCH_WINDOW_DAYS} days of its due date.",
        ))

    # --- leftover credits: unmatched, real money in with no invoice behind it
    for c in pool:
        if c.credit_id not in consumed:
            ledger.add_credit_exception(Exception_.make(
                ExceptionCode.UNMATCHED_CREDIT,
                invoice_id=None, credit_id=c.credit_id, amount_paisa=c.amount_paisa,
                explanation=f"Credit {c.credit_id} ({c.raw_narration!r}) does not match any invoice "
                            "or hypothesis - untracked income, a personal transfer, or a payout from "
                            "another source.",
            ))

    return ledger


def _book_short_paid(ledger: Ledger, inv: Invoice, credit: Credit) -> None:
    gross = gross_amount(inv)
    received = credit.amount_paisa
    shortfall = Paisa(int(gross) - int(received))
    proof = Proof(
        stage="stage3b_short_pay", rule_fired="short_paid", invoice_id=inv.invoice_id,
        credit_id=credit.credit_id,
        fields_compared={"gross_paisa": int(gross), "received_paisa": int(received)},
        residual_paisa=Paisa(0),
    )
    ledger.add(LedgerLine(invoice_id=inv.invoice_id, bucket=Bucket.RECEIVED, amount_paisa=received,
                           note="Partial payment, no lawful deduction basis found.", proof=proof))
    ledger.add(LedgerLine(invoice_id=inv.invoice_id, bucket=Bucket.SHORT, amount_paisa=shortfall,
                           note="Shortfall with no lawful basis.", proof=proof))
    ledger.add_invoice_exception(Exception_.make(
        ExceptionCode.SHORT_PAID,
        invoice_id=inv.invoice_id, credit_id=credit.credit_id, amount_paisa=shortfall,
        explanation=f"Invoice {inv.invoice_id}: Rs {shortfall/100:.2f} short, with no matching "
                    f"lawful deduction hypothesis for the gap.",
    ))


def _book_over_paid(ledger: Ledger, inv: Invoice, credit: Credit) -> None:
    gross = gross_amount(inv)
    excess = Paisa(int(credit.amount_paisa) - int(gross))
    proof = Proof(
        stage="stage3c_over_paid", rule_fired="over_paid", invoice_id=inv.invoice_id,
        credit_id=credit.credit_id,
        fields_compared={"gross_paisa": int(gross), "received_paisa": int(credit.amount_paisa)},
        residual_paisa=Paisa(0),
    )
    # the invoice's own bucket accounting only ever covers its own gross -
    # the excess isn't part of what was invoiced, so it's recorded as an
    # exception (tied to both the invoice and the credit) rather than a
    # fifth ledger bucket. This keeps the conservation law's per-invoice
    # identity exact: RECEIVED alone explains the whole invoice here.
    ledger.add(LedgerLine(invoice_id=inv.invoice_id, bucket=Bucket.RECEIVED, amount_paisa=gross,
                           note="Overpaid - full invoice amount received, plus an excess.", proof=proof))
    ledger.add_invoice_exception(Exception_.make(
        ExceptionCode.OVER_PAID,
        invoice_id=inv.invoice_id, credit_id=credit.credit_id, amount_paisa=excess,
        explanation=f"Invoice {inv.invoice_id}: Rs {excess/100:.2f} more than invoiced arrived in a "
                    f"single credit - confirm before spending it, may be a duplicate payment or an advance.",
    ))


def _book_matched_invoice(ledger: Ledger, batch: Batch, inv: Invoice, hyp: Hypothesis,
                           credit_or_credits, stage: str, ambiguous: bool, alternates: list[str]) -> None:
    client = batch.client(inv.client_id)
    credits = credit_or_credits if isinstance(credit_or_credits, list) else [credit_or_credits]
    credit_ids = [c.credit_id for c in credits]
    gross = gross_amount(inv)

    received = Paisa(int(gross) - int(hyp.deduction_amount_paisa))
    proof = Proof(
        stage=stage, rule_fired=hyp.label, invoice_id=inv.invoice_id,
        credit_id=",".join(credit_ids),
        fields_compared={
            "predicted_net_paisa": int(hyp.predicted_net_paisa),
            "credit_total_paisa": sum(int(c.amount_paisa) for c in credits),
            "ambiguous": ambiguous, "alternates": alternates,
        },
        residual_paisa=Paisa(int(hyp.predicted_net_paisa) - sum(int(c.amount_paisa) for c in credits)),
    )

    ledger.add(LedgerLine(invoice_id=inv.invoice_id, bucket=Bucket.RECEIVED, amount_paisa=received,
                           note=f"Matched via {stage} ({hyp.label}).", proof=proof))

    is_statutory_tds = hyp.deduction_kind.value.startswith("TDS_")
    if int(hyp.deduction_amount_paisa) > 0 and is_statutory_tds:
        entry = _form26as_has(batch, client.tan, hyp.deduction_amount_paisa, inv.issue_date)
        bucket = Bucket.DEDUCTED_CREDITABLE if entry is not None else Bucket.DEDUCTED_UNCREDITABLE
        ledger.add(LedgerLine(invoice_id=inv.invoice_id, bucket=bucket,
                               amount_paisa=hyp.deduction_amount_paisa,
                               note=hyp.explanation, proof=proof))
        if entry is None:
            ledger.add_invoice_exception(Exception_.make(
                ExceptionCode.TDS_NOT_IN_26AS,
                invoice_id=inv.invoice_id, credit_id=credit_ids[0], amount_paisa=hyp.deduction_amount_paisa,
                explanation=f"{hyp.deduction_amount_paisa/100:.2f} deducted from invoice "
                            f"{inv.invoice_id} but no matching entry found in Form 26AS for "
                            f"deductor TAN {client.tan!r}.",
            ))
    elif int(hyp.deduction_amount_paisa) > 0:
        # non-TDS deduction (platform commission, gateway fee) - Form 26AS
        # doesn't apply; it's a real cost, not a tax credit, so it always
        # books to DEDUCTED_CREDITABLE (any rate variance is its own
        # exception, added below via hyp.exception_if_matched).
        ledger.add(LedgerLine(invoice_id=inv.invoice_id, bucket=Bucket.DEDUCTED_CREDITABLE,
                               amount_paisa=hyp.deduction_amount_paisa,
                               note=hyp.explanation, proof=proof))

    if hyp.exception_if_matched is not None and hyp.exception_if_matched != ExceptionCode.TDS_NOT_IN_26AS:
        ledger.add_invoice_exception(Exception_.make(
            hyp.exception_if_matched,
            invoice_id=inv.invoice_id, credit_id=credit_ids[0], amount_paisa=hyp.deduction_amount_paisa,
            explanation=hyp.explanation,
        ))

    if len(credits) > 1:
        ledger.add_invoice_exception(Exception_.make(
            ExceptionCode.SPLIT_PAYMENT,
            invoice_id=inv.invoice_id, credit_id=",".join(credit_ids), amount_paisa=gross,
            explanation=f"Invoice {inv.invoice_id} was settled across {len(credits)} separate credits.",
        ))
