"""Proof object: attached to every matched decision the engine makes."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.money import Paisa


@dataclass(frozen=True, slots=True)
class Proof:
    stage: str                          # which matcher stage produced this (e.g. "stage1_identity")
    rule_fired: str                     # e.g. a hypothesis label, or "utr_exact_match"
    invoice_id: str
    credit_id: str | None
    fields_compared: dict = field(default_factory=dict)
    residual_paisa: Paisa = Paisa(0)    # must be 0 for a RECEIVED match

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "rule_fired": self.rule_fired,
            "invoice_id": self.invoice_id,
            "credit_id": self.credit_id,
            "fields_compared": self.fields_compared,
            "residual_paisa": int(self.residual_paisa),
        }
