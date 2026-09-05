from datetime import date

from core.models import DeductionKind
from core.rules.registry import NO_PAN_OVERRIDE_194O_BPS, NO_PAN_OVERRIDE_BPS, resolve


def test_194j_professional_rate_and_threshold():
    rs = resolve(date(2026, 6, 1))
    rule = rs.rule_for(DeductionKind.TDS_PROFESSIONAL_194J, pan_on_file=True)
    assert rule.rate_bps == 1000            # 10%
    assert rule.threshold_paisa == 50_000 * 100


def test_194j_technical_rate():
    rs = resolve(date(2026, 6, 1))
    rule = rs.rule_for(DeductionKind.TDS_TECHNICAL_194J, pan_on_file=True)
    assert rule.rate_bps == 200              # 2%


def test_no_pan_override():
    rs = resolve(date(2026, 6, 1))
    rule = rs.rule_for(DeductionKind.TDS_PROFESSIONAL_194J, pan_on_file=False)
    assert rule.rate_bps == NO_PAN_OVERRIDE_BPS
    assert rule.rate_bps == 2000             # 20%


def test_194o_rate_and_threshold():
    rs = resolve(date(2026, 6, 1))
    rule = rs.rule_for(DeductionKind.TDS_ECOMMERCE_194O, pan_on_file=True)
    assert rule.rate_bps == 10               # 0.1%
    assert rule.threshold_paisa == 5_00_000 * 100


def test_194o_no_pan_rate_is_5_percent_not_20_percent():
    """
    s.206AA carries a specific proviso for 194-O (inserted by the Finance
    Act 2019, effective 1 Apr 2020) substituting 5% in place of the
    generic 20% no-PAN rate. Applying the generic rate here would
    overstate a real freelancer's deduction by 4x.
    """
    rs = resolve(date(2026, 6, 1))
    rule = rs.rule_for(DeductionKind.TDS_ECOMMERCE_194O, pan_on_file=False)
    assert rule.rate_bps == NO_PAN_OVERRIDE_194O_BPS
    assert rule.rate_bps == 500               # 5%, not the generic 2000 (20%)


def test_financial_year_citation_scheme_changes_at_boundary():
    rs_before = resolve(date(2026, 3, 31))
    rs_after = resolve(date(2026, 4, 1))
    rule_before = rs_before.rule_for(DeductionKind.TDS_PROFESSIONAL_194J)
    rule_after = rs_before is not rs_after and rs_after.rule_for(DeductionKind.TDS_PROFESSIONAL_194J)
    assert rule_before.legacy_citation == "194J"
    assert rule_after.legacy_citation != "194J"
    assert "393" in rule_after.legacy_citation
    # rate is unchanged across the boundary - only the citation differs
    assert rule_before.rate_bps == rule_after.rate_bps
