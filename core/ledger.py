"""
The conservation law.

For every invoice, exactly four buckets - RECEIVED, DEDUCTED_CREDITABLE,
DEDUCTED_UNCREDITABLE, SHORT - must sum to that invoice's gross amount.
This is enforced, not hoped for: `assert_conserves()` raises
LedgerImbalanceError naming the exact invoice and residual the moment
it is violated. This is Gate 1 of the whole project.

Credit-side anomalies (an unmatched credit, a duplicate UTR, an
over-payment) are tracked separately - they are not owed amounts in
the same sense an invoice is, so they do not participate in the
per-invoice identity, but they are still surfaced as exceptions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from core.classify import Exception_
from core.models import Invoice
from core.money import Paisa
from core.proof import Proof


class Bucket(str, Enum):
    RECEIVED = "RECEIVED"
    DEDUCTED_CREDITABLE = "DEDUCTED_CREDITABLE"
    DEDUCTED_UNCREDITABLE = "DEDUCTED_UNCREDITABLE"
    SHORT = "SHORT"


ALL_BUCKETS = (Bucket.RECEIVED, Bucket.DEDUCTED_CREDITABLE, Bucket.DEDUCTED_UNCREDITABLE, Bucket.SHORT)


@dataclass(frozen=True, slots=True)
class LedgerLine:
    invoice_id: str
    bucket: Bucket
    amount_paisa: Paisa
    note: str = ""
    proof: Proof | None = None


class LedgerImbalanceError(Exception):
    pass


class Ledger:
    def __init__(self) -> None:
        self.lines: list[LedgerLine] = []
        self.credit_exceptions: list[Exception_] = []
        self.invoice_exceptions: list[Exception_] = []

    def add(self, line: LedgerLine) -> None:
        self.lines.append(line)

    def add_credit_exception(self, exc: Exception_) -> None:
        self.credit_exceptions.append(exc)

    def add_invoice_exception(self, exc: Exception_) -> None:
        self.invoice_exceptions.append(exc)

    def _by_invoice_bucket(self) -> dict[str, dict[Bucket, Paisa]]:
        out: dict[str, dict[Bucket, Paisa]] = defaultdict(lambda: {b: Paisa(0) for b in ALL_BUCKETS})
        for line in self.lines:
            out[line.invoice_id][line.bucket] = Paisa(out[line.invoice_id][line.bucket] + line.amount_paisa)
        return out

    def assert_conserves(self, invoices: list[Invoice], gross_fn) -> None:
        """
        gross_fn: Invoice -> Paisa (pass core.compose.gross_amount to avoid a
        circular import between core.ledger and core.compose).
        """
        by_invoice = self._by_invoice_bucket()
        problems = []
        for inv in invoices:
            buckets = by_invoice.get(inv.invoice_id, {b: Paisa(0) for b in ALL_BUCKETS})
            total = Paisa(sum(int(v) for v in buckets.values()))
            gross = gross_fn(inv)
            if total != gross:
                problems.append(
                    f"invoice {inv.invoice_id}: buckets sum to {total} paisa but gross is "
                    f"{gross} paisa (residual {gross - total} paisa)"
                )
        if problems:
            raise LedgerImbalanceError(
                f"{len(problems)} invoice(s) fail to reconcile:\n" + "\n".join(problems)
            )

    def totals_by_bucket(self) -> dict[Bucket, Paisa]:
        totals = {b: Paisa(0) for b in ALL_BUCKETS}
        for line in self.lines:
            totals[line.bucket] = Paisa(totals[line.bucket] + line.amount_paisa)
        return totals

    def rupees_accounted_pct(self, invoices: list[Invoice], gross_fn) -> float:
        total_gross = sum(int(gross_fn(inv)) for inv in invoices)
        if total_gross == 0:
            return 100.0
        total_bucketed = sum(int(v) for v in self.totals_by_bucket().values())
        return round(100.0 * total_bucketed / total_gross, 2)

    def auto_match_rate_pct(self, invoices: list[Invoice]) -> float:
        by_invoice = self._by_invoice_bucket()
        if not invoices:
            return 100.0
        auto_matched = 0
        for inv in invoices:
            buckets = by_invoice.get(inv.invoice_id, {b: Paisa(0) for b in ALL_BUCKETS})
            if buckets[Bucket.SHORT] == 0:
                auto_matched += 1
        return round(100.0 * auto_matched / len(invoices), 2)
