"""
Makes real Razorpay TEST-MODE API calls and saves the raw JSON
responses to data/fixtures/razorpay_raw/ - this is what
data/generate.py's synthetic records are modelled on, so the batch is
"synthetic in volume, genuine in structure" rather than a guessed
schema. See plan/baaki.md §4.

Reads credentials from RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET
environment variables ONLY - never hardcode a key/secret in this file
or commit one anywhere in this repo (data/fixtures/razorpay_raw/*.json
is checked to make sure no response body ever contains the secret -
Razorpay's API never echoes it back, but the check costs nothing).

Usage:
    export RAZORPAY_KEY_ID=rzp_test_...
    export RAZORPAY_KEY_SECRET=...
    python data/fetch_razorpay_fixtures.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "fixtures" / "razorpay_raw"
BASE = "https://api.razorpay.com/v1"


class RazorpayNotConfigured(RuntimeError):
    pass


def _auth() -> tuple[str, str]:
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise RazorpayNotConfigured(
            "set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test-mode keys from "
            "dashboard.razorpay.com -> Settings -> API Keys) before running this script."
        )
    if not key_id.startswith("rzp_test_"):
        raise RazorpayNotConfigured(
            f"key_id {key_id!r} does not look like a test-mode key (expected rzp_test_...) - "
            "refusing to run against what might be a live key."
        )
    return key_id, key_secret


class RazorpayCall:
    def __init__(self):
        self.auth = _auth()
        self.log: list[dict] = []

    def _save(self, name: str, payload) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=2)
        # never let a saved fixture contain the secret, even accidentally
        if self.auth[1] in text:
            raise RuntimeError(f"refusing to save {name}: response body contains the key secret")
        (OUT_DIR / f"{name}.json").write_text(text, encoding="utf-8")
        print(f"  saved {name}.json")

    def call(self, name: str, method: str, path: str, **kwargs):
        url = f"{BASE}{path}"
        resp = requests.request(method, url, auth=self.auth, timeout=30, **kwargs)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"_raw_text": resp.text, "_status_code": resp.status_code}
        self._save(name, payload)
        self.log.append({"name": name, "method": method, "path": path, "status": resp.status_code})
        if resp.status_code >= 400:
            print(f"  WARNING: {name} returned {resp.status_code}: {payload}")
        return payload, resp.status_code


def main() -> None:
    rc = RazorpayCall()
    print(f"using key_id: {rc.auth[0]}")
    print()

    # --- Customers ---------------------------------------------------
    # contact/email must be unique per run - Razorpay dedupes customers by
    # these fields and rejects a repeat with "Customer already exists"
    run_tag = str(int(time.time()))[-6:]
    print("1. create customer")
    customer, _ = rc.call(
        "01_create_customer", "POST", "/customers",
        json={
            "name": "Arjun Textiles Pvt Ltd",
            "email": f"arjun.textiles.{run_tag}@example.com",
            "contact": f"98765{run_tag}",
            "notes": {"source": "ghost-rupees-fixture"},
        },
    )
    customer_id = customer.get("id")

    print("2. fetch that customer")
    rc.call("02_fetch_customer", "GET", f"/customers/{customer_id}") if customer_id else None

    print("3. fetch all customers")
    rc.call("03_fetch_all_customers", "GET", "/customers")

    # --- Invoices ------------------------------------------------------
    print("4. create invoice with customer details (no existing customer)")
    inv1, _ = rc.call(
        "04_create_invoice_with_details", "POST", "/invoices",
        json={
            "type": "invoice",
            "customer": {
                "name": "BluePeak Consulting",
                "email": "billing@bluepeak.example.com",
                "contact": "9876500001",
            },
            "line_items": [
                {"name": "Consulting services - September", "amount": 2000000, "currency": "INR", "quantity": 1},
            ],
            "currency": "INR",
            "description": "Ghost Rupees fixture invoice - schema sample",
            "sms_notify": 0,
            "email_notify": 0,
        },
    )
    inv1_id = inv1.get("id")

    if customer_id:
        print("5. create invoice with existing customer_id")
        inv2, _ = rc.call(
            "05_create_invoice_with_customer_id", "POST", "/invoices",
            json={
                "type": "invoice",
                "customer_id": customer_id,
                "line_items": [
                    {"name": "Professional services", "amount": 5500000, "currency": "INR", "quantity": 1},
                ],
                "currency": "INR",
                "sms_notify": 0,
                "email_notify": 0,
            },
        )
        inv2_id = inv2.get("id")
    else:
        inv2_id = None

    if inv1_id:
        # invoices without a `draft` flag are issued immediately on
        # creation (confirmed live: a separate POST .../issue call on an
        # already-issued invoice 400s with "Operation not allowed for
        # Invoice in issued status" - the docs list `draft` as valid but
        # this test account rejected it outright, so this project just
        # accepts the auto-issue behavior rather than fighting it)
        print("6. fetch invoice #1 (already issued on creation)")
        rc.call("06_fetch_invoice", "GET", f"/invoices/{inv1_id}")

    print("8. fetch all invoices")
    rc.call("08_fetch_all_invoices", "GET", "/invoices")

    # --- Payment Links --------------------------------------------------
    print("9. create a standard payment link")
    plink, _ = rc.call(
        "09_create_payment_link_standard", "POST", "/payment_links",
        json={
            "amount": 2875000,
            "currency": "INR",
            "description": "Ghost Rupees fixture - standard payment link",
            "customer": {"name": "Fernhill Media", "email": "pay@fernhillmedia.example.com", "contact": "9876500002"},
            "notify": {"sms": False, "email": False},
        },
    )
    plink_id = plink.get("id")

    print("10. create a UPI payment link")
    rc.call(
        "10_create_payment_link_upi", "POST", "/payment_links",
        json={
            "amount": 1800000,
            "currency": "INR",
            "description": "Ghost Rupees fixture - UPI payment link",
            "upi_link": True,
            "customer": {"name": "Northwind Studios LLP", "contact": "9876500003"},
        },
    )

    if plink_id:
        print("11. fetch that payment link")
        rc.call("11_fetch_payment_link", "GET", f"/payment_links/{plink_id}")

    print("12. fetch all standard payment links")
    rc.call("12_fetch_all_payment_links", "GET", "/payment_links")

    # --- Smart Collect (virtual accounts) --------------------------------
    print("13. create a Smart Collect customer identifier (virtual account)")
    va, _ = rc.call(
        "13_create_smart_collect_identifier", "POST", "/virtual_accounts",
        json={
            "receivers": {"types": ["bank_account"]},
            "description": "Ghost Rupees fixture - Smart Collect identifier",
            "customer_id": customer_id,
            "notes": {"purpose": "schema-fidelity-fixture"},
        } if customer_id else {
            "receivers": {"types": ["bank_account"]},
            "description": "Ghost Rupees fixture - Smart Collect identifier",
        },
    )
    va_id = va.get("id")

    if va_id:
        print("14. fetch that Smart Collect identifier")
        rc.call("14_fetch_smart_collect_identifier", "GET", f"/virtual_accounts/{va_id}")

        print("15. fetch payments for that Smart Collect identifier")
        rc.call("15_fetch_payments_for_identifier", "GET", f"/virtual_accounts/{va_id}/payments")

    print("16. fetch all Smart Collect identifiers")
    rc.call("16_fetch_all_smart_collect_identifiers", "GET", "/virtual_accounts")

    # --- Orders (foundational to the payments flow) -----------------------
    print("17. create an order")
    order, _ = rc.call(
        "17_create_order", "POST", "/orders",
        json={"amount": 4500000, "currency": "INR", "receipt": "ghost-rupees-fixture-1", "notes": {"source": "fixture"}},
    )

    print("18. fetch all payments (test account, likely empty)")
    rc.call("18_fetch_all_payments", "GET", "/payments")

    print()
    print(f"done: {len(rc.log)} calls made, saved to {OUT_DIR}")
    manifest = {"calls": rc.log, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (OUT_DIR / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("wrote _manifest.json")


if __name__ == "__main__":
    try:
        main()
    except RazorpayNotConfigured as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
