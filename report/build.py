"""
Builds a single self-contained HTML report from a Batch + Ledger.

This exists so a reviewer can open one file and see the result without
running any code - the committed fallback artifact if `python cli.py run`
isn't run live in front of them.
"""

from __future__ import annotations

import html
from collections import Counter
from pathlib import Path

from core.classify import Exception_
from core.compose import gross_amount
from core.ledger import Bucket, Ledger
from core.models import Batch
from core.money import paisa_to_rupees_str

_CSS = """
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:980px;margin:32px auto;padding:0 20px;
     color:#131a19;background:#f6f7f4;line-height:1.5}
h1{font-size:1.9rem;margin-bottom:2px} .sub{color:#55605c;margin-top:0}
.stats{display:flex;gap:14px;flex-wrap:wrap;margin:22px 0}
.stat{background:#fff;border:1px solid #dce1db;border-radius:6px;padding:14px 18px;min-width:170px}
.stat .n{font-size:1.5rem;font-weight:700;font-variant-numeric:tabular-nums}
.stat .l{font-size:.8rem;color:#55605c}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:.92rem;background:#fff}
th,td{border-bottom:1px solid #dce1db;padding:8px 10px;text-align:left}
th{background:#eff1ed;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:#55605c}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.code{font-family:ui-monospace,Menlo,monospace;font-size:.85rem}
.pill{display:inline-block;padding:2px 8px;border-radius:3px;font-size:.72rem;font-family:ui-monospace,monospace}
.pill.ghost{background:#f6e5e0;color:#a93520}
.pill.credit{background:#e3ede8;color:#17564b}
h2{border-top:1px solid #dce1db;padding-top:22px;margin-top:36px}
.callout{background:#e3ede8;border:1px solid #17564b;border-radius:6px;padding:16px 20px;margin:20px 0}
"""


def _esc(x) -> str:
    return html.escape(str(x))


def _exception_rows(exceptions: list[Exception_]) -> str:
    rows = []
    for e in sorted(exceptions, key=lambda e: -int(e.amount_paisa)):
        rows.append(
            f"<tr><td class='code pill ghost'>{_esc(e.code.value)}</td>"
            f"<td class='code'>{_esc(e.invoice_id or '-')}</td>"
            f"<td class='code'>{_esc(e.credit_id or '-')}</td>"
            f"<td class='num'>Rs {paisa_to_rupees_str(e.amount_paisa)}</td>"
            f"<td>{_esc(e.explanation)}</td>"
            f"<td>{_esc(e.action)}</td></tr>"
        )
    return "\n".join(rows)


def build_report(batch: Batch, ledger: Ledger, out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    auto_match = ledger.auto_match_rate_pct(batch.invoices)
    accounted = ledger.rupees_accounted_pct(batch.invoices, gross_amount)
    totals = ledger.totals_by_bucket()

    inv_code_counts = Counter(e.code.value for e in ledger.invoice_exceptions)
    cred_code_counts = Counter(e.code.value for e in ledger.credit_exceptions)

    ghost = next((e for e in ledger.invoice_exceptions if e.invoice_id == "INV-GHOST-01"
                  and e.code.value == "TDS_NOT_IN_26AS"), None)
    ghost_html = ""
    if ghost is not None:
        ghost_html = (
            "<div class='callout'><strong>The worked example (INV-GHOST-01):</strong> "
            f"Rs {paisa_to_rupees_str(ghost.amount_paisa)} was deducted from this invoice and never "
            "appeared in Form 26AS. {}</div>"
        ).format(_esc(ghost.explanation))

    bucket_rows = "\n".join(
        f"<tr><td class='code'>{_esc(b.value)}</td><td class='num'>Rs {paisa_to_rupees_str(totals[b])}</td></tr>"
        for b in Bucket
    )
    inv_count_rows = "\n".join(
        f"<tr><td class='code'>{_esc(code)}</td><td class='num'>{n}</td></tr>"
        for code, n in inv_code_counts.most_common()
    )
    cred_count_rows = "\n".join(
        f"<tr><td class='code'>{_esc(code)}</td><td class='num'>{n}</td></tr>"
        for code, n in cred_code_counts.most_common()
    )

    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Ghost Rupees - Reconciliation Report</title>
<style>{_CSS}</style></head>
<body>
<h1>Ghost Rupees</h1>
<p class="sub">Every rupee you invoiced — received, deducted, or still owed.</p>

<div class="stats">
  <div class="stat"><div class="n">{len(batch.invoices)}</div><div class="l">invoices</div></div>
  <div class="stat"><div class="n">{len(batch.credits)}</div><div class="l">credits</div></div>
  <div class="stat"><div class="n">{auto_match:.2f}%</div><div class="l">auto-match rate</div></div>
  <div class="stat"><div class="n">{accounted:.2f}%</div><div class="l">rupees accounted for</div></div>
</div>

{ghost_html}

<h2>The four buckets</h2>
<table><tr><th>Bucket</th><th>Amount</th></tr>{bucket_rows}</table>

<h2>Invoice exceptions ({len(ledger.invoice_exceptions)})</h2>
<table><tr><th>Count by code</th><th></th></tr>{inv_count_rows}</table>
<table>
<tr><th>Code</th><th>Invoice</th><th>Credit</th><th>Amount</th><th>Explanation</th><th>Action</th></tr>
{_exception_rows(ledger.invoice_exceptions)}
</table>

<h2>Credit exceptions ({len(ledger.credit_exceptions)})</h2>
<table><tr><th>Count by code</th><th></th></tr>{cred_count_rows}</table>
<table>
<tr><th>Code</th><th>Invoice</th><th>Credit</th><th>Amount</th><th>Explanation</th><th>Action</th></tr>
{_exception_rows(ledger.credit_exceptions)}
</table>

</body></html>
"""
    out_path.write_text(html_doc, encoding="utf-8")
