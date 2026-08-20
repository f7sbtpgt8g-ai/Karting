"""Part 4 tests: templated narrative sentences and headline ranking. The
Anthropic-assisted phrasing path is exercised separately (it falls back to
templated_sentence with no API key set, which is the state in CI/tests)."""

import pandas as pd

from telemetry.narrative import anthropic_phrasing, narrative_sentence, rank_headline_findings, templated_sentence


def _row(**overrides):
    base = dict(
        corner_label="Corner 1", pattern_type="compromised_exit_fast_entry", confidence="medium",
        net_time_impact_s=0.25, entry_speed_delta_kmh=5.0, apex_speed_delta_kmh=2.0, exit_speed_delta_kmh=-3.0,
        zone_a_delta_s=-0.02, zone_b_delta_s=-0.15, zone_c_delta_s=0.30, headline=True,
        evidence={"entry_speed_delta_kmh": 5.0, "zone_b_delta_s": -0.15, "zone_c_delta_s": 0.30, "exit_speed_delta_kmh": -3.0},
        root_cause_corner=None,
    )
    base.update(overrides)
    return base


def test_templated_sentence_mentions_corner_and_is_nonempty_for_every_known_pattern():
    for pattern in [
        "compromised_exit_fast_entry", "conservative_entry_strong_exit", "early_apex_exit_compromised",
        "late_apex_exit_rewarded", "clean_no_significant_delta", "unclassified_time_delta",
    ]:
        row = _row(pattern_type=pattern)
        sentence = templated_sentence(row)
        assert row["corner_label"] in sentence
        assert len(sentence) > 0


def test_templated_sentence_braking_point_subtypes():
    earlier = _row(
        pattern_type="braking_point_delta_no_pace_change",
        evidence={"entry_distance_delta_m": -8.0, "subtype": "earlier_with_margin"},
    )
    later = _row(
        pattern_type="braking_point_delta_no_pace_change",
        evidence={"entry_distance_delta_m": 8.0, "subtype": "later_no_cost"},
    )
    assert "margin" in templated_sentence(earlier)
    assert "isn't costing" in templated_sentence(later)


def test_templated_sentence_appends_root_cause_note():
    row = _row(root_cause_corner="Corner 3")
    sentence = templated_sentence(row)
    assert "Corner 3" in sentence
    assert "complex" in sentence


def test_anthropic_phrasing_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert anthropic_phrasing(_row()) is None


def test_narrative_sentence_falls_back_to_templated_without_anthropic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sentence = narrative_sentence(_row(), use_anthropic=True)
    assert sentence == templated_sentence(_row())


def test_rank_headline_findings_excludes_clean_and_sorts_by_net_impact():
    comparisons = pd.DataFrame(
        [
            _row(corner_label="Corner 1", net_time_impact_s=0.10, headline=True),
            _row(corner_label="Corner 2", pattern_type="clean_no_significant_delta", net_time_impact_s=0.0, headline=False),
            _row(corner_label="Corner 3", net_time_impact_s=-0.40, headline=True),
        ]
    )
    findings = rank_headline_findings(comparisons, n=5)
    assert [f["corner_label"] for f in findings] == ["Corner 3", "Corner 1"]
    assert all("narrative" in f for f in findings)
