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

Stage 2 (UTR lookup via Razorpay's "Fetch Payments Using UTR" API) is
implemented as a clearly-labelled seam: it takes an injectable
resolver that is a no-op by default (wiring it to the live API is a
one-function job, deliberately left for when real Razorpay
credentials are available).
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


def _stage1_identity(invoice: Invoice, pool: list[Credit]) -> Credit | None:
    for c in pool:
        if c.razorpay_payment_id and c.razorpay_payment_id in invoice.notes:
            return c
        if c.razorpay_customer_identifier and c.razorpay_customer_identifier in invoice.notes:
            return c
    return None


def _stage2_utr(invoice: Invoice, pool: list[Credit],
                 utr_resolver: Callable[[str], str | None]) -> Credit | None:
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
) -> tuple[Credit, Hypothesis] | None:
    """
    Cross-credit ties (two or more DISTINCT credits predicting the same
    net for this invoice - a genuine cross-client collision, not just
    two hypotheses on the same credit) are resolved in two tiers,
    cheapest first:

    1. Deterministic: does a candidate's raw narration directly contain
       this client's (compacted, letters-only) name as a substring? No
       model call, free, and resolves the common case where the bank
       narration spells the name out in full.
    2. narration_hint: an LLM-parsed counterparty name (from
       llm.narration, passed in by the CALLER as plain tuples - never
       imported directly here, see the module docstring and
       tests/test_import_boundary.py) for cases where tier 1 fails.

    Failing both, this falls back to picking whichever credit was
    encountered first.
    """
    candidates = [c for c in pool if _within_window(invoice, c.value_date)]
    matches: list[tuple[Credit, Hypothesis]] = []
    for c in candidates:
        for h in hyps:
            if int(c.amount_paisa) == int(h.predicted_net_paisa):
                matches.append((c, h))
    if not matches:
        return None

    seen_ids: set[str] = set()
    distinct_credit_ids: list[str] = []
    for c, _ in matches:
        if c.credit_id not in seen_ids:
            seen_ids.add(c.credit_id)
            distinct_credit_ids.append(c.credit_id)
    chosen_credit_id = distinct_credit_ids[0]

    if len(distinct_credit_ids) > 1 and client_name:
        compact_client = _compact(client_name)
        # tier 1: deterministic substring match against raw narration
        substring_matched = [
            cid for cid in distinct_credit_ids
            if compact_client and compact_client in _compact(next(c for c, _ in matches if c.credit_id == cid).raw_narration)
        ]
        if len(substring_matched) == 1:
            chosen_credit_id = substring_matched[0]
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

    return next((c, h) for c, h in matches if c.credit_id == chosen_credit_id)


SHORT_PAY_MIN_FRACTION = 0.5   # a candidate short-pay credit must be at least
                                # this fraction of gross, or it's more likely
                                # an unrelated small credit than a short-pay


def _stage3b_short_pay(invoice: Invoice, hyps: list[Hypothesis],
                        pool: list[Credit]) -> Credit | None:
    """
    Not every gap has a lawful explanation. A client can simply pay less
    than invoiced with no deduction basis at all. Stage 3 (exact
    hypothesis match) already failed by the time this runs, so any
    candidate here is, by construction, an amount that matches NO lawful
    or common-error hypothesis - the defining feature of a short payment.
    """
    gross = int(gross_amount(invoice))
    floor = int(gross * SHORT_PAY_MIN_FRACTION)
    candidates = [
        c for c in pool
        if _within_window(invoice, c.value_date) and floor <= int(c.amount_paisa) < gross
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


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


def _compact(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _stage4_split(invoice: Invoice, hyps: list[Hypothesis],
                   pool: list[Credit], client_name: str = "") -> tuple[list[Credit], Hypothesis] | None:
    """
    One invoice settled across up to MAX_SET_SIZE credits. Scoped to
    credits whose narration names this client - unscoped subset-sum over
    an entire multi-client credit pool WILL eventually find a spurious
    combination that happens to sum correctly (confirmed empirically on
    the 12-defect holdout batch: it found a 3-credit "match" built from
    three different clients' unrelated payments). See DECISIONS.md.
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

        if credit is not None:
            hyp = next((h for h in hyps if int(h.predicted_net_paisa) == int(credit.amount_paisa)), None)
            if hyp is None:
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
                credit, hyp = found

        if credit is not None and hyp is not None:
            consumed.add(credit.credit_id)
            _book_matched_invoice(ledger, batch, inv, hyp, credit, stage)
            continue

        short_credit = _stage3b_short_pay(inv, hyps, [c for c in pool if c.credit_id not in consumed])
        if short_credit is not None:
            consumed.add(short_credit.credit_id)
            _book_short_paid(ledger, inv, short_credit)
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
            _book_matched_invoice(ledger, batch, inv, hyp, credits, "stage4_split")
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
                    _book_matched_invoice(ledger, batch, inv, per_invoice_hyp[inv.invoice_id], credit, "stage4_merge")
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


def _book_matched_invoice(ledger: Ledger, batch: Batch, inv: Invoice, hyp: Hypothesis,
                           credit_or_credits, stage: str) -> None:
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
        # doesn't apply; it always books to DEDUCTED_CREDITABLE (any rate
        # variance is its own exception, added below via hyp.exception_if_matched).
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
