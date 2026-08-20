"""Direct tests of the Part 2 classification taxonomy against hand-built
`CornerComparison` facts -- one scenario per named pattern, each engineered
to clear (or, for `inconclusive`, deliberately not clear) exactly one
rule's thresholds, using the default `SignificanceThresholds`.
"""

from telemetry.pattern_rules import CornerComparison, SignificanceThresholds, classify_corner

TH = SignificanceThresholds()


def _cmp(**overrides) -> CornerComparison:
    base = dict(
        corner_label="Corner 1",
        entry_speed_delta_kmh=0.0, apex_speed_delta_kmh=0.0, exit_speed_delta_kmh=0.0,
        entry_distance_delta_m=0.0, apex_distance_delta_m=0.0,
        zone_a_delta_s=0.0, zone_b_delta_s=0.0, zone_c_delta_s=0.0,
        lap_entry_speed_kmh=100.0, ref_entry_speed_kmh=100.0,
        lap_exit_speed_kmh=100.0, ref_exit_speed_kmh=100.0,
    )
    base.update(overrides)
    return CornerComparison(**base)


def test_compromised_exit_fast_entry():
    cmp = _cmp(entry_speed_delta_kmh=5.0, zone_b_delta_s=-0.15, zone_c_delta_s=0.30)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "compromised_exit_fast_entry"
    assert match.headline is True
    assert match.net_time_impact_s > 0  # a net loss despite the mid-corner gain


def test_conservative_entry_strong_exit():
    cmp = _cmp(entry_speed_delta_kmh=-5.0, zone_a_delta_s=0.02, zone_b_delta_s=0.03, zone_c_delta_s=-0.20)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "conservative_entry_strong_exit"
    assert match.net_time_impact_s < 0  # a net gain


def test_early_apex_exit_compromised():
    cmp = _cmp(apex_distance_delta_m=-10.0, apex_speed_delta_kmh=0.5, exit_speed_delta_kmh=-3.0, zone_c_delta_s=0.10, zone_b_delta_s=0.02)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "early_apex_exit_compromised"


def test_late_apex_exit_rewarded():
    cmp = _cmp(apex_distance_delta_m=8.0, exit_speed_delta_kmh=4.0, zone_c_delta_s=-0.10, zone_b_delta_s=-0.01)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "late_apex_exit_rewarded"


def test_braking_point_delta_earlier_with_margin():
    cmp = _cmp(entry_distance_delta_m=-10.0, zone_a_delta_s=0.01, zone_b_delta_s=0.02, zone_c_delta_s=-0.01)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "braking_point_delta_no_pace_change"
    assert match.evidence["subtype"] == "earlier_with_margin"


def test_braking_point_delta_later_no_cost():
    cmp = _cmp(entry_distance_delta_m=10.0, zone_a_delta_s=-0.01, zone_b_delta_s=0.01, zone_c_delta_s=0.02)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "braking_point_delta_no_pace_change"
    assert match.evidence["subtype"] == "later_no_cost"


def test_clean_no_significant_delta():
    cmp = _cmp(
        entry_speed_delta_kmh=0.2, apex_speed_delta_kmh=0.1, exit_speed_delta_kmh=0.1,
        entry_distance_delta_m=1.0, apex_distance_delta_m=1.0,
        zone_a_delta_s=0.01, zone_b_delta_s=0.02, zone_c_delta_s=0.01,
    )
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "clean_no_significant_delta"
    assert match.headline is False


def test_unclassified_time_delta_fallback():
    # No specific rule's trigger conditions are met, but the zones/net delta
    # are clearly real and significant -- must still be surfaced, just
    # without a specific causal story.
    cmp = _cmp(entry_distance_delta_m=1.0, zone_a_delta_s=0.2, zone_b_delta_s=0.2, zone_c_delta_s=0.2)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "unclassified_time_delta"
    assert match.confidence == "low"
    assert match.headline is True


def test_inconclusive_when_nothing_clears_any_threshold():
    # Just above the "clean" zone-time floor but not enough net delta to
    # clear the generic-fallback bar either -- neither a finding nor "clean".
    cmp = _cmp(zone_a_delta_s=0.06, zone_b_delta_s=0.06, zone_c_delta_s=-0.02)
    match = classify_corner(cmp, TH)
    assert match.pattern_type == "inconclusive"
    assert match.headline is False


def test_classify_corner_always_returns_a_match():
    # Every corner must get a classification, even an all-zero comparison.
    match = classify_corner(_cmp(), TH)
    assert match.pattern_type == "clean_no_significant_delta"
