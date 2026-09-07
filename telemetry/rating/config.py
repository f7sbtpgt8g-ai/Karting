"""Every tunable the batch-side rating computation uses, in one place.

Matches `pattern_rules.SignificanceThresholds`'s shape: a plain dataclass
with defaults that are reasoned starting points, not measured constants.
These are genuinely meant to move once there's real multi-driver data to
watch -- nothing here is load-bearing enough to justify hiding it inside
the functions that use it.

Display-side tunables (sigma decay rate, grace window, provisional
threshold, k in `mu - k*sigma`) are a *separate* config, in
web/src/lib/rating.ts -- not duplicated here. The batch job never computes
decay (see 0015's driver_ratings comment: decay is a pure function of time,
evaluated at display time), so there is no shared constant to keep in sync
between the two beyond what each file's docstring cross-references.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RatingConfig:
    # -- Part 1: validity gate -----------------------------------------
    # A lap under this fraction of its track+class's median (clean-lap)
    # time is physically implausible and held for review rather than
    # trusted or silently dropped.
    implausible_floor_factor: float = 0.75
    # Need at least this many clean laps at a track+class before a floor
    # is even computed -- otherwise one early lap could flag itself.
    implausible_floor_min_sample: int = 5

    # Track-cut check: how many resampled points make up a track's
    # reference line (uniform arc-length fraction 0..1 of the lap).
    reference_line_points: int = 120
    # A reference line needs at least this many verified laps to be built
    # at all -- too few and "the median lap" is really just one lap.
    reference_line_min_laps: int = 8
    # Per-point tolerance = max(floor, k * MAD of the laps that built the
    # reference, at that point) -- a floor because GPS jitter alone is a
    # few metres even on a lap that cut nothing.
    reference_line_tolerance_floor_m: float = 3.0
    reference_line_tolerance_k: float = 4.0
    # A lap flags as a likely track cut once at least this many resampled
    # points fall outside their tolerance -- a single point is more likely
    # a GPS glitch than a cut.
    track_cut_min_offending_points: int = 3

    # Speed-drop-without-braking: a frame-to-frame speed drop (km/h) at
    # least this large, while the stored braking_estimate is False for the
    # whole drop, reads as a sensor glitch/spin/off rather than a real
    # braking event this logger just didn't tag.
    speed_drop_threshold_kmh: float = 25.0

    # -- Part 2: field-relative pace scoring -----------------------------
    # A cohort needs at least this many *other* verified sessions (so
    # cohort size >= this + 1) before it's treated as a real field;
    # otherwise falls back to the historical reference.
    min_cohort_others: int = 2
    # Elo K-factor, before dividing by (cohort_size - 1) to average across
    # every pairing rather than let a bigger field linearly inflate the
    # update.
    k_factor: float = 32.0
    # Elo expected-score formula's scale divisor (the "400" in the
    # standard `10^(diff/400)`).
    elo_scale: float = 400.0

    # How many of a bucket's best verified sessions the historical
    # reference is the median of -- robust against a single outlier best-
    # ever lap, per the brief's explicit "not a single best-ever outlier".
    reference_top_n: int = 5
    # A bucket needs at least this many distinct verified sessions before
    # a reference value is computed for it at all.
    reference_min_sessions: int = 3

    # -- Part 3: sigma shrink (growth/decay is display-side, see above) --
    # Multiplicative shrink applied to sigma on every full-weight
    # contributing verified session: sigma *= (1 - this).
    sigma_shrink_rate: float = 0.10
    # Never shrinks below this -- a driver with hundreds of sessions still
    # has *some* residual uncertainty, not a false-certain number.
    sigma_floor: float = 40.0

    # -- Part 4: streaks ---------------------------------------------
    # A week counts as "qualifying" once it has at least this many
    # verified sessions.
    streak_min_sessions_per_week: int = 1
    # A week with at least this many verified sessions earns the cosmetic
    # "trained mid-week" badge -- purely display, never touches mu/sigma.
    midweek_bonus_min_sessions: int = 2
    # A contributing verified session beyond the first one in the same
    # driver-week still shrinks sigma, just at this fraction of the full
    # rate -- diminishing returns on volume, not zero and not full credit.
    midweek_sigma_weight: float = 0.30
    # Freezes: how many a new driver starts with, the cap on how many can
    # be banked at once, and how many consecutive qualifying weeks earn
    # the next one. A single missed week consumes one freeze if available
    # (streak survives); two consecutive missed weeks always breaks it
    # regardless of freezes banked.
    streak_initial_freezes: int = 1
    streak_max_banked_freezes: int = 2
    streak_freeze_earn_interval_weeks: int = 8


DEFAULT_RATING_CONFIG = RatingConfig()
