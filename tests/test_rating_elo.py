"""Part 2/3: field-relative pace scoring, Elo-style updates, and the
sigma-shrink half of confidence -- against synthetic sessions, no database.
"""

from __future__ import annotations

from datetime import date

from telemetry.rating.config import RatingConfig
from telemetry.rating.elo import (
    FIELD,
    REFERENCE,
    RatingState,
    SessionPace,
    compute_reference_bucket,
    expected_score,
    field_based_update,
    group_into_cohorts,
    reference_based_update,
)
from telemetry.rating.engine import _parse_session_date

CONFIG = RatingConfig()
DAY = date(2026, 6, 1)


def _pace(driver_profile_id: int, pace_s: float, **overrides) -> SessionPace:
    defaults = dict(
        session_db_id=driver_profile_id * 100,
        driver_profile_id=driver_profile_id,
        pace_s=pace_s,
        track_name="Test Track",
        session_date=DAY,
        conditions="Dry",
        engine_category="Senior",
    )
    defaults.update(overrides)
    return SessionPace(**defaults)


def test_expected_score_is_half_for_equal_ratings():
    assert expected_score(1500, 1500, CONFIG) == 0.5


def test_expected_score_favours_the_higher_rating():
    assert expected_score(1600, 1500, CONFIG) > 0.5
    assert expected_score(1500, 1600, CONFIG) < 0.5


def test_expected_score_is_symmetric():
    a = expected_score(1700, 1400, CONFIG)
    b = expected_score(1400, 1700, CONFIG)
    assert abs(a + b - 1.0) < 1e-9


def test_group_into_cohorts_splits_by_track_date_conditions_class():
    sessions = [
        _pace(1, 30.0),
        _pace(2, 31.0),
        _pace(3, 30.5, conditions="Wet"),
        _pace(4, 29.0, track_name="Other Track"),
    ]
    cohorts = group_into_cohorts(sessions)
    assert len(cohorts) == 3
    dry_cohort = cohorts[("Test Track", DAY, "Dry", "Senior")]
    assert {s.driver_profile_id for s in dry_cohort} == {1, 2}


def test_fastest_in_a_field_of_equals_gains_rating():
    cohort = [_pace(1, 30.0), _pace(2, 31.0), _pace(3, 32.0)]
    ratings = {1: RatingState(1500, 350), 2: RatingState(1500, 350), 3: RatingState(1500, 350)}
    updates = field_based_update(cohort, ratings, config=CONFIG)
    by_driver = {u.driver_profile_id: u for u in updates}
    assert by_driver[1].mu_after > by_driver[1].mu_before  # fastest gains
    assert by_driver[3].mu_after < by_driver[3].mu_before  # slowest loses
    assert all(u.mechanism == FIELD for u in updates)
    assert all(u.cohort_size == 3 for u in updates)


def test_beating_a_stronger_field_gains_more_than_beating_a_weak_one():
    strong_cohort = [_pace(1, 30.0), _pace(2, 30.1)]
    strong_ratings = {1: RatingState(1500, 350), 2: RatingState(1700, 350)}
    strong_updates = {u.driver_profile_id: u for u in field_based_update(strong_cohort, strong_ratings, config=CONFIG)}

    weak_cohort = [_pace(1, 30.0), _pace(2, 30.1)]
    weak_ratings = {1: RatingState(1500, 350), 2: RatingState(1300, 350)}
    weak_updates = {u.driver_profile_id: u for u in field_based_update(weak_cohort, weak_ratings, config=CONFIG)}

    strong_gain = strong_updates[1].mu_after - strong_updates[1].mu_before
    weak_gain = weak_updates[1].mu_after - weak_updates[1].mu_before
    assert strong_gain > weak_gain > 0


def test_field_update_sigma_shrinks_by_full_weight_by_default():
    cohort = [_pace(1, 30.0), _pace(2, 31.0)]
    ratings = {1: RatingState(1500, 350), 2: RatingState(1500, 350)}
    updates = field_based_update(cohort, ratings, config=CONFIG)
    for u in updates:
        assert u.sigma_after == u.sigma_before * (1 - CONFIG.sigma_shrink_rate)


def test_field_update_sigma_shrink_respects_reduced_midweek_weight():
    cohort = [_pace(1, 30.0), _pace(2, 31.0)]
    ratings = {1: RatingState(1500, 350), 2: RatingState(1500, 350)}
    weights = {100: CONFIG.midweek_sigma_weight}  # driver 1's session_db_id is 100
    updates = {u.driver_profile_id: u for u in field_based_update(cohort, ratings, weights, CONFIG)}
    full_shrink = 350 * (1 - CONFIG.sigma_shrink_rate)
    partial_shrink = 350 * (1 - CONFIG.sigma_shrink_rate * CONFIG.midweek_sigma_weight)
    assert updates[1].sigma_after == partial_shrink
    assert updates[2].sigma_after == full_shrink
    assert updates[1].sigma_after > full_shrink  # reduced, not zero, shrink


def test_sigma_never_shrinks_below_the_floor():
    cohort = [_pace(1, 30.0), _pace(2, 40.0)]
    ratings = {1: RatingState(1500, CONFIG.sigma_floor + 0.001), 2: RatingState(1500, CONFIG.sigma_floor + 0.001)}
    updates = field_based_update(cohort, ratings, config=CONFIG)
    assert all(u.sigma_after >= CONFIG.sigma_floor for u in updates)


def test_reference_bucket_needs_a_minimum_sample():
    sessions = [_pace(i, 30.0 + i) for i in range(CONFIG.reference_min_sessions - 1)]
    assert compute_reference_bucket(sessions, {}, CONFIG) is None


def test_reference_bucket_is_the_median_of_the_top_n_and_implied_rating_is_their_mean_mu():
    sessions = [_pace(1, 29.0), _pace(2, 30.0), _pace(3, 31.0)]
    mus = {1: 1600.0, 2: 1500.0, 3: 1400.0}
    bucket = compute_reference_bucket(sessions, mus, CONFIG)
    assert bucket is not None
    assert bucket.reference_lap_s == 30.0  # median of [29, 30, 31]
    assert bucket.implied_rating == 1500.0  # mean of 1600/1500/1400


def test_beating_the_reference_gains_rating_losing_to_it_loses_rating():
    from telemetry.rating.elo import ReferenceBucket

    bucket = ReferenceBucket(
        track_name="Test Track", engine_category="Senior", conditions="Dry",
        reference_lap_s=30.0, implied_rating=1500.0, sample_session_count=5,
    )
    faster = reference_based_update(_pace(1, 29.0), bucket, RatingState(1500, 350), config=CONFIG)
    slower = reference_based_update(_pace(1, 31.0), bucket, RatingState(1500, 350), config=CONFIG)
    assert faster.mu_after > faster.mu_before
    assert slower.mu_after < slower.mu_before
    assert faster.mechanism == REFERENCE
    assert faster.cohort_size == 0


class TestParseSessionDate:
    """`sessions.start_date` is written verbatim from the Unipro export's
    own "Start Date" column (telemetry/parser.py), so its shape follows
    whatever the logger/locale produced -- not a single guaranteed format.
    A real deployment surfaced this: every session used YYYY-MM-DD, which
    the original DD-MM-YYYY-only parser silently couldn't read, so every
    session was dropped before it ever reached a cohort -- laps validity-
    checked, zero pace buckets, zero rating updates, no visible error."""

    def test_dd_mm_yyyy(self):
        assert _parse_session_date("29-08-2026") == date(2026, 8, 29)

    def test_yyyy_mm_dd(self):
        assert _parse_session_date("2026-08-29") == date(2026, 8, 29)

    def test_none_and_empty(self):
        assert _parse_session_date(None) is None
        assert _parse_session_date("") is None

    def test_unrecognized_format_returns_none_rather_than_raising(self):
        assert _parse_session_date("29/08/2026") is None
