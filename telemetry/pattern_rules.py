"""Rule-based causal attribution for corner-by-corner comparisons (Part 2 of
the corner-by-corner causal coaching engine).

Deterministic and inspectable by design: every rule here is a plain function
taking the same `CornerComparison` facts and `SignificanceThresholds`
config, returning a `PatternMatch` or `None`. Adding a new pattern to the
taxonomy is adding a new function to `TAXONOMY_RULES` -- never restructuring
the engine that runs them.

Thresholds are deliberately not hard-coded guesses baked into the rule
bodies: `SignificanceThresholds` is a config object, tunable per call, with
defaults chosen to require "a few hundredths of a second" per zone before a
finding is worth mentioning at all (per the spec this module implements
against) and refined further by `corner_engine.calibrate_thresholds` once a
driver has enough of their own repeat-lap data to measure real noise from.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SignificanceThresholds:
    """Minimum-detectable-difference floors below which a delta is treated
    as noise, not a finding. Defaults are reasoned starting points, not
    measured constants -- tune via `corner_engine.calibrate_thresholds` once
    a driver has several sessions of their own data to measure real
    lap-to-lap variance from."""

    # Per-zone time delta, in seconds, below which a zone is "unchanged".
    min_zone_time_delta_s: float = 0.05
    # Net (zone B + zone C) time delta, in seconds, below which a corner's
    # overall net impact isn't worth a headline finding.
    min_net_time_delta_s: float = 0.08
    # GPS speed delta, in km/h, below which entry/apex/exit speed is
    # "unchanged" -- GPS speed on a typical consumer logger has a few
    # tenths of a km/h of sample-to-sample jitter even on a truly constant
    # speed, so 1.0 km/h is a conservative floor above that.
    min_speed_delta_kmh: float = 1.0
    # GPS-distance delta, in metres, below which an entry/apex point is
    # "at the same place" -- GPS position jitter plus the ~10Hz fix rate
    # means sub-3m differences aren't reliably a real different point.
    min_distance_delta_m: float = 3.0
    # A braking-point difference has to clear a higher bar than the
    # general distance floor before it's called out as its own finding
    # (rather than just noise in exactly where braking happened to start).
    braking_point_delta_m: float = 6.0


@dataclass
class CornerComparison:
    """The complete set of deterministic facts for one corner, lap vs.
    reference -- the only thing the classification rules below are allowed
    to look at."""

    corner_label: str
    entry_speed_delta_kmh: float
    apex_speed_delta_kmh: float
    exit_speed_delta_kmh: float
    entry_distance_delta_m: float
    apex_distance_delta_m: float
    zone_a_delta_s: float
    zone_b_delta_s: float
    zone_c_delta_s: float
    lap_entry_speed_kmh: float
    ref_entry_speed_kmh: float
    lap_exit_speed_kmh: float
    ref_exit_speed_kmh: float

    @property
    def net_delta_s(self) -> float:
        """Zone B (whole corner) + zone C (following straight) -- zone A is
        a subset of zone B, so it's excluded here to avoid double-counting;
        this is the "how much time did this corner+straight actually cost
        or gain overall" figure findings are ranked by."""
        return self.zone_b_delta_s + self.zone_c_delta_s


def build_corner_comparison(
    corner_label: str, lap_points: dict, ref_points: dict, lap_zones: dict, ref_zones: dict
) -> CornerComparison:
    """Assemble a `CornerComparison` from one corner's extracted points +
    zone times for the analyzed lap and the reference lap (each a dict-like
    row from `corner_causal.corner_points_for_lap` / `three_zone_times`)."""
    return CornerComparison(
        corner_label=corner_label,
        entry_speed_delta_kmh=lap_points["entry_speed_kmh"] - ref_points["entry_speed_kmh"],
        apex_speed_delta_kmh=lap_points["apex_speed_kmh"] - ref_points["apex_speed_kmh"],
        exit_speed_delta_kmh=lap_points["exit_speed_kmh"] - ref_points["exit_speed_kmh"],
        entry_distance_delta_m=lap_points["entry_distance_m"] - ref_points["entry_distance_m"],
        apex_distance_delta_m=lap_points["apex_distance_m"] - ref_points["apex_distance_m"],
        zone_a_delta_s=lap_zones["zone_a_time_s"] - ref_zones["zone_a_time_s"],
        zone_b_delta_s=lap_zones["zone_b_time_s"] - ref_zones["zone_b_time_s"],
        zone_c_delta_s=lap_zones["zone_c_time_s"] - ref_zones["zone_c_time_s"],
        lap_entry_speed_kmh=lap_points["entry_speed_kmh"], ref_entry_speed_kmh=ref_points["entry_speed_kmh"],
        lap_exit_speed_kmh=lap_points["exit_speed_kmh"], ref_exit_speed_kmh=ref_points["exit_speed_kmh"],
    )


@dataclass
class PatternMatch:
    corner_label: str
    pattern_type: str
    confidence: str  # "low" | "medium" | "high"
    net_time_impact_s: float
    headline: bool  # eligible for the ranked headline-findings list, vs. table-only
    evidence: dict = field(default_factory=dict)


def rule_compromised_exit_fast_entry(cmp: CornerComparison, th: SignificanceThresholds) -> PatternMatch | None:
    """Entered faster than reference, gained time through the corner, but
    lost more than that down the following straight -- the canonical
    "fast entry compromised the exit" pattern."""
    if not (cmp.entry_speed_delta_kmh > th.min_speed_delta_kmh):
        return None
    if not (cmp.zone_b_delta_s < -th.min_zone_time_delta_s):  # gained time through the corner
        return None
    if not (cmp.zone_c_delta_s > th.min_zone_time_delta_s):  # lost time on the straight
        return None
    if not (cmp.zone_c_delta_s > abs(cmp.zone_b_delta_s)):  # the straight loss exceeds the corner gain
        return None
    if not (cmp.net_delta_s > th.min_net_time_delta_s):  # and it's a real net loss, not a wash
        return None
    return PatternMatch(
        corner_label=cmp.corner_label, pattern_type="compromised_exit_fast_entry", confidence="medium",
        net_time_impact_s=cmp.net_delta_s, headline=True,
        evidence={
            "entry_speed_delta_kmh": cmp.entry_speed_delta_kmh, "zone_b_delta_s": cmp.zone_b_delta_s,
            "zone_c_delta_s": cmp.zone_c_delta_s, "exit_speed_delta_kmh": cmp.exit_speed_delta_kmh,
        },
    )


def rule_conservative_entry_strong_exit(cmp: CornerComparison, th: SignificanceThresholds) -> PatternMatch | None:
    """Entered slower than reference, gave up little or nothing through the
    corner, and that setup a stronger exit that paid for itself (and more)
    down the straight."""
    if not (cmp.entry_speed_delta_kmh < -th.min_speed_delta_kmh):
        return None
    if not (cmp.zone_c_delta_s < -th.min_zone_time_delta_s):  # gained time on the straight
        return None
    corner_cost = max(cmp.zone_a_delta_s, cmp.zone_b_delta_s, 0.0)
    if not (abs(cmp.zone_c_delta_s) > corner_cost):  # the straight gain outweighs any corner-entry cost
        return None
    if not (cmp.net_delta_s < -th.min_net_time_delta_s):
        return None
    return PatternMatch(
        corner_label=cmp.corner_label, pattern_type="conservative_entry_strong_exit", confidence="medium",
        net_time_impact_s=cmp.net_delta_s, headline=True,
        evidence={
            "entry_speed_delta_kmh": cmp.entry_speed_delta_kmh, "zone_a_delta_s": cmp.zone_a_delta_s,
            "zone_b_delta_s": cmp.zone_b_delta_s, "zone_c_delta_s": cmp.zone_c_delta_s,
        },
    )


def rule_early_apex_exit_compromised(cmp: CornerComparison, th: SignificanceThresholds) -> PatternMatch | None:
    """Apex point earlier (by distance) than reference, apex speed similar
    or higher, but exit speed down and time lost on the straight --
    "using up the corner early"."""
    if not (cmp.apex_distance_delta_m < -th.min_distance_delta_m):
        return None
    if not (cmp.apex_speed_delta_kmh >= -th.min_speed_delta_kmh):
        return None
    if not (cmp.exit_speed_delta_kmh < -th.min_speed_delta_kmh):
        return None
    if not (cmp.zone_c_delta_s > th.min_zone_time_delta_s):
        return None
    return PatternMatch(
        corner_label=cmp.corner_label, pattern_type="early_apex_exit_compromised", confidence="medium",
        net_time_impact_s=cmp.net_delta_s, headline=True,
        evidence={
            "apex_distance_delta_m": cmp.apex_distance_delta_m, "apex_speed_delta_kmh": cmp.apex_speed_delta_kmh,
            "exit_speed_delta_kmh": cmp.exit_speed_delta_kmh, "zone_c_delta_s": cmp.zone_c_delta_s,
        },
    )


def rule_late_apex_exit_rewarded(cmp: CornerComparison, th: SignificanceThresholds) -> PatternMatch | None:
    """Apex point later than reference and exit speed / straight time both
    improved -- a later apex that paid off."""
    if not (cmp.apex_distance_delta_m > th.min_distance_delta_m):
        return None
    if not (cmp.exit_speed_delta_kmh > th.min_speed_delta_kmh):
        return None
    if not (cmp.zone_c_delta_s < -th.min_zone_time_delta_s):
        return None
    return PatternMatch(
        corner_label=cmp.corner_label, pattern_type="late_apex_exit_rewarded", confidence="medium",
        net_time_impact_s=cmp.net_delta_s, headline=True,
        evidence={
            "apex_distance_delta_m": cmp.apex_distance_delta_m, "exit_speed_delta_kmh": cmp.exit_speed_delta_kmh,
            "zone_c_delta_s": cmp.zone_c_delta_s,
        },
    )


def rule_braking_point_delta_no_pace_change(cmp: CornerComparison, th: SignificanceThresholds) -> PatternMatch | None:
    """Braking point clearly earlier/later than reference, but the rest of
    the corner and the following straight are statistically unchanged --
    a braking-point habit independent of any exit story, either "overslowing
    with margin to spare" (braked earlier, nothing gained or lost) or "a
    later/more aggressive brake that didn't cost anything"."""
    if not (abs(cmp.entry_distance_delta_m) > th.braking_point_delta_m):
        return None
    if not (
        abs(cmp.zone_a_delta_s) < th.min_zone_time_delta_s
        and abs(cmp.zone_b_delta_s) < th.min_zone_time_delta_s
        and abs(cmp.zone_c_delta_s) < th.min_zone_time_delta_s
    ):
        return None
    subtype = "earlier_with_margin" if cmp.entry_distance_delta_m < 0 else "later_no_cost"
    return PatternMatch(
        corner_label=cmp.corner_label, pattern_type="braking_point_delta_no_pace_change", confidence="medium",
        net_time_impact_s=cmp.net_delta_s, headline=True,
        evidence={"entry_distance_delta_m": cmp.entry_distance_delta_m, "subtype": subtype},
    )


def rule_clean_no_significant_delta(cmp: CornerComparison, th: SignificanceThresholds) -> PatternMatch | None:
    """Every zone and the net delta are within the noise floor -- don't
    manufacture a finding; this corner is statistically indistinguishable
    from the reference lap."""
    if (
        abs(cmp.zone_a_delta_s) < th.min_zone_time_delta_s
        and abs(cmp.zone_b_delta_s) < th.min_zone_time_delta_s
        and abs(cmp.zone_c_delta_s) < th.min_zone_time_delta_s
        and abs(cmp.net_delta_s) < th.min_net_time_delta_s
    ):
        return PatternMatch(
            corner_label=cmp.corner_label, pattern_type="clean_no_significant_delta", confidence="high",
            net_time_impact_s=cmp.net_delta_s, headline=False, evidence={},
        )
    return None


def rule_generic_net_delta(cmp: CornerComparison, th: SignificanceThresholds) -> PatternMatch | None:
    """Fallback: a real, significant net time delta that doesn't match any
    named pattern above -- still surfaced (so a genuine finding is never
    silently dropped), but at low confidence and without a specific causal
    story attached."""
    if abs(cmp.net_delta_s) > th.min_net_time_delta_s:
        return PatternMatch(
            corner_label=cmp.corner_label, pattern_type="unclassified_time_delta", confidence="low",
            net_time_impact_s=cmp.net_delta_s, headline=True,
            evidence={
                "zone_a_delta_s": cmp.zone_a_delta_s, "zone_b_delta_s": cmp.zone_b_delta_s,
                "zone_c_delta_s": cmp.zone_c_delta_s,
            },
        )
    return None


# Priority-ordered: the first matching rule wins. `rule_clean_no_significant_delta`
# and `rule_generic_net_delta` are deliberately last -- specific causal
# stories should win over "no finding" or "unclassified" whenever the data
# supports one.
TAXONOMY_RULES = [
    rule_compromised_exit_fast_entry,
    rule_conservative_entry_strong_exit,
    rule_early_apex_exit_compromised,
    rule_late_apex_exit_rewarded,
    rule_braking_point_delta_no_pace_change,
    rule_clean_no_significant_delta,
    rule_generic_net_delta,
]


def classify_corner(cmp: CornerComparison, thresholds: SignificanceThresholds | None = None) -> PatternMatch:
    """Run the taxonomy against one corner's comparison facts, returning the
    first matching pattern. Always returns a match -- if nothing above fired
    (small deltas that don't clear the "clean" bar either, e.g. exactly one
    zone borderline), a low-confidence, non-headline "inconclusive" result
    is returned rather than raising, so callers never have to special-case
    "no classification"."""
    thresholds = thresholds or SignificanceThresholds()
    for rule in TAXONOMY_RULES:
        match = rule(cmp, thresholds)
        if match is not None:
            return match
    return PatternMatch(
        corner_label=cmp.corner_label, pattern_type="inconclusive", confidence="low",
        net_time_impact_s=cmp.net_delta_s, headline=False, evidence={},
    )
