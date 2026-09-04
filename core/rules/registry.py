"""
FY-versioned tax/deduction rule tables.

*** VERIFY EVERY RATE BELOW AGAINST THE INCOME TAX DEPARTMENT'S OWN ***
*** MATERIAL BEFORE RELYING ON THIS IN ANYTHING REAL. ***

These are taken from secondary tax-advisory sources current as of
August 2026 (see plan/baaki.md §7 and §20 for the source list) and
cross-checked across several, but not against the primary IT Department
publication. Every Rule below carries a `citation` field precisely so
this can be checked and updated in one place.

Two things this registry exists to get right, on purpose:

1. Rates are stored as integer basis points and thresholds as integer
   paisa - never floats - because this module feeds core.money, which
   refuses floats outright.
2. Rules are versioned by financial year. From 1 April 2026, TDS
   provisions are mapped under the Income Tax Act, 2025 (transactions
   reference the relevant Section 393 table item rather than the old
   194-series section numbers), at broadly unchanged rates. A payment
   in March 2026 cites the old section; a payment in June 2026 cites
   the new table item, at the same rate. `resolve()` picks the right
   FinancialYear (and therefore the right citation) for a given date;
   the *rate* itself does not change between the two years modelled
   here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from core.models import DeductionKind
from core.money import BasisPoints, Paisa


class DeductionBase(str, Enum):
    EXCLUSIVE_OF_GST = "EXCLUSIVE_OF_GST"   # rate applies to the pre-GST amount (correct, standard)
    INCLUSIVE_OF_GST = "INCLUSIVE_OF_GST"   # rate applied to the GST-inclusive total (a common over-deduction error)


@dataclass(frozen=True, slots=True)
class Rule:
    code: DeductionKind
    rate_bps: BasisPoints
    threshold_paisa: Paisa
    base: DeductionBase
    legacy_citation: str
    verification_status: str = "UNVERIFIED - confirm against IT Dept before production use"


@dataclass(frozen=True, slots=True)
class FinancialYear:
    label: str
    start: date
    end: date
    citation_scheme: str   # "income-tax-act-1961" (194-series) or "income-tax-act-2025" (s.393)

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


FY_2025_26 = FinancialYear(
    label="FY2025-26",
    start=date(2025, 4, 1),
    end=date(2026, 3, 31),
    citation_scheme="income-tax-act-1961",
)

FY_2026_27 = FinancialYear(
    label="FY2026-27",
    start=date(2026, 4, 1),
    end=date(2027, 3, 31),
    citation_scheme="income-tax-act-2025",
)

_FINANCIAL_YEARS = [FY_2025_26, FY_2026_27]


def financial_year_for(d: date) -> FinancialYear:
    for fy in _FINANCIAL_YEARS:
        if fy.contains(d):
            return fy
    raise ValueError(f"no FinancialYear modelled for date {d.isoformat()}")


def _citation(scheme: str, legacy: str, s393_item: str) -> str:
    return legacy if scheme == "income-tax-act-1961" else s393_item


# --- Rate table, rates fixed across both modelled financial years -----
# (rates are unchanged FY25-26 -> FY26-27; only the citation scheme differs)

_RATES: dict[DeductionKind, dict[str, object]] = {
    DeductionKind.TDS_PROFESSIONAL_194J: dict(
        rate_bps=BasisPoints(1000),                     # 10.00%
        threshold_paisa=Paisa(50_000 * 100),             # Rs 50,000
        base=DeductionBase.EXCLUSIVE_OF_GST,
        legacy="194J", s393="s.393 Table Item (professional fees)",
    ),
    DeductionKind.TDS_TECHNICAL_194J: dict(
        rate_bps=BasisPoints(200),                       # 2.00%
        threshold_paisa=Paisa(50_000 * 100),             # Rs 50,000
        base=DeductionBase.EXCLUSIVE_OF_GST,
        legacy="194J", s393="s.393 Table Item (technical/call-centre fees)",
    ),
    DeductionKind.TDS_CONTRACT_194C: dict(
        rate_bps=BasisPoints(100),                       # 1.00% (individual/HUF payee)
        threshold_paisa=Paisa(30_000 * 100),             # Rs 30,000 single payment
        base=DeductionBase.EXCLUSIVE_OF_GST,
        legacy="194C", s393="s.393 Table Item (contract payments)",
    ),
    DeductionKind.TDS_COMMISSION_194H: dict(
        rate_bps=BasisPoints(200),                       # 2.00%
        threshold_paisa=Paisa(20_000 * 100),             # Rs 20,000
        base=DeductionBase.EXCLUSIVE_OF_GST,
        legacy="194H", s393="s.393 Table Item (commission/brokerage)",
    ),
    DeductionKind.TDS_ECOMMERCE_194O: dict(
        rate_bps=BasisPoints(10),                        # 0.10%
        threshold_paisa=Paisa(5_00_000 * 100),           # Rs 5,00,000 aggregate/FY
        base=DeductionBase.EXCLUSIVE_OF_GST,
        legacy="194-O", s393="s.393 Table Item (e-commerce operator payments)",
    ),
}

# No-PAN override (s.206AA / successor): 20% flat, overrides the section rate.
NO_PAN_OVERRIDE_BPS = BasisPoints(2000)

# GST
GST_DOMESTIC_BPS = BasisPoints(1800)     # 18.00%, domestic services
GST_EXPORT_BPS = BasisPoints(0)          # 0%, zero-rated under LUT
GST_REGISTRATION_THRESHOLD_PAISA = Paisa(20_00_000 * 100)  # Rs 20 lakh, services


class RuleSet:
    """The resolved rule table for one financial year."""

    def __init__(self, fy: FinancialYear):
        self.fy = fy

    def rule_for(self, kind: DeductionKind, *, pan_on_file: bool = True) -> Rule | None:
        if kind == DeductionKind.NONE:
            return None
        spec = _RATES.get(kind)
        if spec is None:
            return None
        rate_bps = spec["rate_bps"]
        if not pan_on_file:
            rate_bps = NO_PAN_OVERRIDE_BPS
        return Rule(
            code=kind,
            rate_bps=rate_bps,
            threshold_paisa=spec["threshold_paisa"],
            base=spec["base"],
            legacy_citation=_citation(self.fy.citation_scheme, spec["legacy"], spec["s393"]),
        )

    def all_deduction_kinds(self) -> list[DeductionKind]:
        return list(_RATES.keys())


def resolve(payment_date: date) -> RuleSet:
    """The single entry point the rest of the engine should use."""
    return RuleSet(financial_year_for(payment_date))
