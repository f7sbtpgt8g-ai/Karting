"""Part 2/3: field-relative pace scoring and the sigma-shrink half of
confidence tracking. (Sigma *growth* -- decay since a driver's last verified
session -- is display-side, see web/src/lib/rating.ts; nothing here mutates
sigma except in response to an actual contributing session.)

The core idea, straight from the brief: don't score a session against an
absolute reference time as the primary signal -- score it against the other
drivers who actually drove the same conditions that day. Every pairing in
that day's cohort is treated as a virtual head-to-head match, Elo's
expected-score formula says who "should" have won it given current ratings,
and the gap between that and what actually happened moves mu. Fastest in a
tough, higher-rated field on a miserable day beats expectation regardless of
the stopwatch in isolation; fastest in a weak field barely moves the needle,
because it was expected.

Pure functions taking plain data in, no database access -- `engine.py` is
the only thing that queries Postgres and feeds these.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import DEFAULT_RATING_CONFIG, RatingConfig

FIELD = "field"
REFERENCE = "reference"


@dataclass
class SessionPace:
    """One verified session's contribution to a day's cohort: its best
    verified lap, and the four fields that define "the same field" --
    track, calendar date, conditions, and class."""

    session_db_id: int
    driver_profile_id: int
    pace_s: float
    track_name: str
    session_date: date
    conditions: str
    engine_category: str


@dataclass
class RatingState:
    mu: float
    sigma: float


@dataclass
class RatingUpdate:
    driver_profile_id: int
    session_db_id: int
    mechanism: str
    cohort_track: str | None
    cohort_date: date | None
    cohort_conditions: str | None
    cohort_class: str | None
    cohort_size: int
    mu_before: float
    mu_after: float
    sigma_before: float
    sigma_after: float
    note: str | None


CohortKey = tuple[str, date, str, str]


def expected_score(rating_a: float, rating_b: float, config: RatingConfig = DEFAULT_RATING_CONFIG) -> float:
    """Standard Elo expected-score formula: the probability `a` "wins" this
    pairing, given the two current ratings."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / config.elo_scale))


def group_into_cohorts(sessions: list[SessionPace]) -> dict[CohortKey, list[SessionPace]]:
    """Every verified session, grouped by the four fields that define "the
    same field that day": track, calendar date, conditions, class."""
    cohorts: dict[CohortKey, list[SessionPace]] = {}
    for s in sessions:
        key = (s.track_name, s.session_date, s.conditions, s.engine_category)
        cohorts.setdefault(key, []).append(s)
    return cohorts


def _shrink_sigma(sigma: float, weight: float, config: RatingConfig = DEFAULT_RATING_CONFIG) -> float:
    """Multiplicative shrink toward the floor, scaled by `weight` (1.0 for a
    driver-week's first contributing session, `midweek_sigma_weight` for
    subsequent ones that same week -- see streaks.py) -- more evidence,
    more certainty, but extra volume in one week counts for less than a
    session in a week that would otherwise have none."""
    effective_rate = config.sigma_shrink_rate * weight
    return max(config.sigma_floor, sigma * (1.0 - effective_rate))


def field_based_update(
    cohort: list[SessionPace],
    ratings: dict[int, RatingState],
    session_weights: dict[int, float] | None = None,
    config: RatingConfig = DEFAULT_RATING_CONFIG,
) -> list[RatingUpdate]:
    """One virtual round-robin: every driver in `cohort` plays every other
    driver once, "wins" if their pace was better (lower time), and mu moves
    by the average gap between actual and expected outcome across all of
    that driver's pairings -- averaged, not summed, so a bigger field
    doesn't linearly multiply the swing purely from being bigger.

    Requires len(cohort) >= 2; callers are expected to have already
    filtered out cohorts too small to count as a field (see
    config.min_cohort_others) and routed those through
    `reference_based_update` instead.
    """
    n = len(cohort)
    updates: list[RatingUpdate] = []
    weights = session_weights or {}
    for driver in cohort:
        state = ratings[driver.driver_profile_id]
        total_gap = 0.0
        for other in cohort:
            if other.driver_profile_id == driver.driver_profile_id:
                continue
            other_state = ratings[other.driver_profile_id]
            actual = 1.0 if driver.pace_s < other.pace_s else (0.5 if driver.pace_s == other.pace_s else 0.0)
            expected = expected_score(state.mu, other_state.mu, config)
            total_gap += actual - expected
        delta_mu = config.k_factor * (total_gap / (n - 1))
        mu_after = state.mu + delta_mu
        sigma_after = _shrink_sigma(state.sigma, weights.get(driver.session_db_id, 1.0), config)
        updates.append(
            RatingUpdate(
                driver_profile_id=driver.driver_profile_id,
                session_db_id=driver.session_db_id,
                mechanism=FIELD,
                cohort_track=driver.track_name,
                cohort_date=driver.session_date,
                cohort_conditions=driver.conditions,
                cohort_class=driver.engine_category,
                cohort_size=n,
                mu_before=state.mu,
                mu_after=mu_after,
                sigma_before=state.sigma,
                sigma_after=sigma_after,
                note=f"delta_mu={delta_mu:+.1f} across {n - 1} pairing(s)",
            )
        )
    return updates


@dataclass
class ReferenceBucket:
    track_name: str
    engine_category: str
    conditions: str
    reference_lap_s: float
    implied_rating: float
    sample_session_count: int


def compute_reference_bucket(
    top_sessions: list[SessionPace], driver_mus: dict[int, float], config: RatingConfig = DEFAULT_RATING_CONFIG
) -> ReferenceBucket | None:
    """The historical reference for one track+class+conditions bucket:
    median of the top `reference_top_n` verified session paces (robust
    against a single best-ever outlier), and an implied opponent rating --
    the mean current mu of the drivers who posted those top times -- so the
    reference can stand in as a single virtual opponent in the same
    Elo-expected-score formula the field-based path uses.

    `top_sessions` is expected to already be sorted fastest-first and
    truncated to config.reference_top_n by the caller (engine.py), since
    that selection needs the full history for the bucket, not just what's
    in memory for one nightly batch pass.
    """
    if len(top_sessions) < config.reference_min_sessions:
        return None
    first = top_sessions[0]
    times = sorted(s.pace_s for s in top_sessions)
    mid = len(times) // 2
    reference_lap_s = times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2.0
    mus = [driver_mus[s.driver_profile_id] for s in top_sessions if s.driver_profile_id in driver_mus]
    implied_rating = sum(mus) / len(mus) if mus else 1500.0
    return ReferenceBucket(
        track_name=first.track_name,
        engine_category=first.engine_category,
        conditions=first.conditions,
        reference_lap_s=reference_lap_s,
        implied_rating=implied_rating,
        sample_session_count=len(top_sessions),
    )


def reference_based_update(
    session: SessionPace,
    bucket: ReferenceBucket,
    state: RatingState,
    session_weight: float = 1.0,
    config: RatingConfig = DEFAULT_RATING_CONFIG,
) -> RatingUpdate:
    """A solo/no-field session, scored against the historical reference
    treated as a single virtual opponent -- same win/lose-vs-expectation
    mechanism as the field-based path, just with one opponent instead of a
    cohort."""
    actual = 1.0 if session.pace_s < bucket.reference_lap_s else (0.5 if session.pace_s == bucket.reference_lap_s else 0.0)
    expected = expected_score(state.mu, bucket.implied_rating, config)
    delta_mu = config.k_factor * (actual - expected)
    mu_after = state.mu + delta_mu
    sigma_after = _shrink_sigma(state.sigma, session_weight, config)
    return RatingUpdate(
        driver_profile_id=session.driver_profile_id,
        session_db_id=session.session_db_id,
        mechanism=REFERENCE,
        cohort_track=session.track_name,
        cohort_date=session.session_date,
        cohort_conditions=session.conditions,
        cohort_class=session.engine_category,
        cohort_size=0,
        mu_before=state.mu,
        mu_after=mu_after,
        sigma_before=state.sigma,
        sigma_after=sigma_after,
        note=(
            f"vs reference {bucket.reference_lap_s:.3f}s "
            f"(implied rating {bucket.implied_rating:.0f}, {bucket.sample_session_count} sample sessions), "
            f"delta_mu={delta_mu:+.1f}"
        ),
    )
