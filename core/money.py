"""
Integer paisa, everywhere. No floats are permitted in the money path.

A rupee amount that touches a float anywhere between ingestion and the
final ledger is how reconciliation tools silently drift by a paisa on
large batches and nobody notices until an audit. Every amount in this
codebase is an int, denominated in paisa (1 rupee = 100 paisa), from the
moment it is parsed until the moment it is printed.
"""

from __future__ import annotations

import re
from typing import NewType

Paisa = NewType("Paisa", int)
BasisPoints = NewType("BasisPoints", int)  # 10000 bps = 100.00%

_RUPEE_STRING_RE = re.compile(r"^\s*-?(\d[\d,]*)(\.(\d{1,2}))?\s*$")


class MoneyError(ValueError):
    pass


def rupees_to_paisa(value: str | int | float) -> Paisa:
    """
    Parse a rupee-denominated value into integer paisa.

    Deliberately rejects raw floats: a float rupee amount (23600.0) has
    already lost the guarantee that it represents an exact number of
    paisa, and passing one silently would defeat the entire point of
    this module. Callers must pass a string ("23600.00") or an int
    number of *rupees* they are certain is exact.
    """
    if isinstance(value, float):
        raise MoneyError(
            f"refusing to convert a float to Paisa: {value!r}. "
            "Pass a string (\"236.00\") or an exact int number of rupees."
        )
    if isinstance(value, int):
        return Paisa(value * 100)

    m = _RUPEE_STRING_RE.match(value)
    if not m:
        raise MoneyError(f"not a valid rupee amount: {value!r}")
    whole = m.group(1).replace(",", "")
    frac = (m.group(3) or "").ljust(2, "0")
    sign = -1 if value.strip().startswith("-") else 1
    return Paisa(sign * (int(whole) * 100 + int(frac)))


def paisa_to_rupees_str(amount: Paisa) -> str:
    """Format paisa as a rupee string with 2 decimal places, e.g. '236.00'."""
    sign = "-" if amount < 0 else ""
    a = abs(int(amount))
    return f"{sign}{a // 100}.{a % 100:02d}"


def apply_bps(amount: Paisa, bps: BasisPoints) -> Paisa:
    """
    Apply a basis-point rate to a paisa amount, rounding half up to the
    nearest paisa. Integer arithmetic throughout - no float ever appears.
    """
    numerator = int(amount) * int(bps)
    # round half up on the /10000 division
    return Paisa((numerator + 5000) // 10000 if numerator >= 0 else -((-numerator + 5000) // 10000))


def gst_inclusive(base: Paisa, gst_bps: BasisPoints) -> Paisa:
    """base + GST on base, e.g. base=20000.00, 18% -> 23600.00"""
    return Paisa(base + apply_bps(base, gst_bps))


def sum_paisa(amounts: "list[Paisa]") -> Paisa:
    return Paisa(sum(int(a) for a in amounts))
