"""
The Smart Collect A/B is the measured evidence behind "Razorpay is
essential, not decorative" - these tests pin the result down so it
can't silently drift into a less honest (or less dramatic, or
accidentally fabricated) number without a test noticing.
"""

from core.compose import gross_amount
from core.ledger import Bucket
from core.match import run_matcher
from eval.smart_collect_ab import build_run_a_anonymous_upi, build_run_b_smart_collect


def test_both_runs_conserve():
    batch_a = build_run_a_anonymous_upi(seed=7, n=40)
    ledger_a = run_matcher(batch_a)
    ledger_a.assert_conserves(batch_a.invoices, gross_amount)   # must not raise

    batch_b = build_run_b_smart_collect(seed=7, n=40)
    ledger_b = run_matcher(batch_b)
    ledger_b.assert_conserves(batch_b.invoices, gross_amount)   # must not raise


def test_smart_collect_never_worse_than_anonymous():
    """The whole point - Smart Collect identity resolution can only
    help or be neutral, never hurt, since it's strictly more
    information than an anonymous credit carries."""
    batch_a = build_run_a_anonymous_upi(seed=7, n=40)
    rate_a = run_matcher(batch_a).auto_match_rate_pct(batch_a.invoices)

    batch_b = build_run_b_smart_collect(seed=7, n=40)
    rate_b = run_matcher(batch_b).auto_match_rate_pct(batch_b.invoices)

    assert rate_b >= rate_a


def test_smart_collect_resolves_every_invoice_via_certain_identity():
    """Every credit in Run B carries its own client's identifier, so
    stage 1 (identity) should resolve all of them - no amount/date
    guessing, no possibility of the cross-client collision class of
    bug this project spent real time fixing."""
    batch_b = build_run_b_smart_collect(seed=7, n=40)
    ledger_b = run_matcher(batch_b)
    ledger_b.assert_conserves(batch_b.invoices, gross_amount)

    identity_matches = [
        l for l in ledger_b.lines
        if l.bucket == Bucket.RECEIVED and l.proof and l.proof.stage == "stage1_identity"
    ]
    assert len(identity_matches) == len(batch_b.invoices)


def test_anonymous_run_has_genuine_collisions_to_struggle_with():
    """Sanity check on the test data itself: the deliberately-injected
    amount collisions must actually produce a meaningfully lower
    auto-match rate in Run A, or this A/B isn't testing anything real."""
    batch_a = build_run_a_anonymous_upi(seed=7, n=40)
    rate_a = run_matcher(batch_a).auto_match_rate_pct(batch_a.invoices)
    assert rate_a < 90.0, (
        f"Run A auto-match rate is {rate_a}% - too high to demonstrate a real gap; "
        "the deliberate collision injection in _build_common_batch may not be firing"
    )
