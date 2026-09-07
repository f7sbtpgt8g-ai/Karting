"""Orchestration: the one thing that actually queries Postgres and writes
back into the tables `supabase/migrations/0015_driver_rating.sql` defines.
Everything math-shaped lives in `validity.py` / `elo.py` / `streaks.py` as
pure, independently-tested functions; this module's job is just wiring real
rows into and out of them, in the right order, idempotently.

Run via `scripts/compute_ratings.py` as a standalone batch job -- never
called synchronously from the upload path (worker/processor.py). Safe to
re-run: laps already validity-checked are skipped (pass `recheck_all=True`
to force), and sessions that already produced a `driver_rating_history` row
are never re-applied.

Phase order matters and is enforced by `run_rating_batch`:
  1. Rebuild each track's GPS reference line, from currently-clean laps
     (is_outlier=False, excluded_by_user=False -- not `validity_status`,
     which is what phase 2 is about to *set*; using it here would be
     circular on a fresh database with nothing validated yet).
  2. Classify every not-yet-checked lap (Part 1) using those reference
     lines plus a per track+class implausible-time floor.
  3. Rebuild the historical pace-reference bucket per track+class+
     conditions (Part 2's fallback), from all-time verified sessions.
  4. Walk verified, public-eligible sessions in chronological order,
     applying field-based or reference-based rating updates per cohort,
     and shrinking sigma per contributing session (Part 2/3).
  5. Rebuild weekly activity + streak state for every driver touched in
     step 4 (Part 4).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime

import numpy as np

from telemetry import db as pgdb

from . import elo, streaks, validity
from .config import DEFAULT_RATING_CONFIG, RatingConfig

logger = logging.getLogger("telemetry.rating.engine")

# sessions.start_date is free text, written verbatim from the Unipro
# export's own "Start Date" column (telemetry/parser.py never reformats
# it) -- so its shape follows whatever the logger/locale that produced the
# export used, not a single guaranteed format. DD-MM-YYYY is what 0011's
# parse_session_date (SQL) assumes and what an earlier version of this
# function assumed too; YYYY-MM-DD shows up in the wild as well (confirmed
# against a real deployment where every session used it, which meant
# _parse_session_date silently dropped every single session before this
# fix -- validity-checked laps, zero pace buckets, zero rating updates).
# Tried in order; the first one that actually matches wins.
_START_DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d")


def _parse_session_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in _START_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Phase 1: validity gate
# ---------------------------------------------------------------------------


def rebuild_track_reference_lines(conn, config: RatingConfig = DEFAULT_RATING_CONFIG) -> int:
    df = pgdb.read_sql(
        conn,
        """
        SELECT s.track_name, lt.session_db_id, lt.lap_number,
               lt.distance_m, lt.latitude, lt.longitude
          FROM lap_traces lt
          JOIN laps l ON l.session_db_id = lt.session_db_id AND l.lap_number = lt.lap_number
          JOIN sessions s ON s.id = lt.session_db_id
         WHERE l.is_outlier = FALSE AND l.excluded_by_user = FALSE
           AND lt.latitude IS NOT NULL AND lt.longitude IS NOT NULL AND lt.distance_m IS NOT NULL
           AND s.track_name IS NOT NULL
        """,
    )
    built = 0
    cur = conn.cursor()
    for track_name, group in df.groupby("track_name"):
        traces = [
            validity.LapPositionTrace(distance_m=row.distance_m, latitude=row.latitude, longitude=row.longitude)
            for row in group.itertuples()
            if row.distance_m and len(row.distance_m) > 1
        ]
        reference = validity.build_reference_line(traces, config)
        if reference is None:
            continue
        cur.execute(
            """
            INSERT INTO track_reference_lines (track_name, ref_lat, ref_lon, ref_tolerance_m, built_from_lap_count, computed_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (track_name) DO UPDATE SET
                ref_lat = EXCLUDED.ref_lat, ref_lon = EXCLUDED.ref_lon,
                ref_tolerance_m = EXCLUDED.ref_tolerance_m,
                built_from_lap_count = EXCLUDED.built_from_lap_count, computed_at = now()
            """,
            (
                track_name,
                [float(x) for x in reference.lat],
                [float(x) for x in reference.lon],
                [float(x) for x in reference.tolerance_m],
                reference.built_from_lap_count,
            ),
        )
        built += 1
    conn.commit()
    logger.info("rebuilt %d track reference line(s)", built)
    return built


def _implausible_floors(conn, config: RatingConfig = DEFAULT_RATING_CONFIG) -> dict[tuple[str, str], float]:
    df = pgdb.read_sql(
        conn,
        """
        SELECT s.track_name, COALESCE(s.engine_category, '') AS engine_category, l.lap_time_s
          FROM laps l JOIN sessions s ON s.id = l.session_db_id
         WHERE l.is_outlier = FALSE AND l.excluded_by_user = FALSE AND l.lap_time_s IS NOT NULL
           AND s.track_name IS NOT NULL
        """,
    )
    floors: dict[tuple[str, str], float] = {}
    for (track_name, engine_category), group in df.groupby(["track_name", "engine_category"]):
        floor = validity.implausible_floor(group["lap_time_s"].tolist(), config)
        if floor is not None:
            floors[(track_name, engine_category)] = floor
    return floors


def run_validity_gate(
    conn, config: RatingConfig = DEFAULT_RATING_CONFIG, recheck_all: bool = False
) -> int:
    """Classify every not-yet-checked lap (or every lap, if `recheck_all`).
    Must run after `rebuild_track_reference_lines` in the same pass."""
    floors = _implausible_floors(conn, config)

    references_df = pgdb.read_sql(conn, "SELECT track_name, ref_lat, ref_lon, ref_tolerance_m, built_from_lap_count FROM track_reference_lines")
    references: dict[str, validity.ReferenceLine] = {
        row.track_name: validity.ReferenceLine(
            lat=np.array(row.ref_lat, dtype=float),
            lon=np.array(row.ref_lon, dtype=float),
            tolerance_m=np.array(row.ref_tolerance_m, dtype=float),
            built_from_lap_count=row.built_from_lap_count,
        )
        for row in references_df.itertuples()
    }

    where = "" if recheck_all else "WHERE l.validity_checked_at IS NULL"
    df = pgdb.read_sql(
        conn,
        f"""
        SELECT l.id AS lap_id, l.lap_time_s, l.is_outlier, l.outlier_reason, l.excluded_by_user,
               s.track_name, COALESCE(s.engine_category, '') AS engine_category,
               lt.distance_m, lt.latitude, lt.longitude, lt.speed_kmh, lt.braking
          FROM laps l
          JOIN sessions s ON s.id = l.session_db_id
          LEFT JOIN lap_traces lt ON lt.session_db_id = l.session_db_id AND lt.lap_number = l.lap_number
        {where}
        """,
    )

    cur = conn.cursor()
    checked = 0
    for row in df.itertuples():
        position = None
        if row.distance_m and row.latitude and row.longitude and len(row.distance_m) > 1:
            position = validity.LapPositionTrace(distance_m=row.distance_m, latitude=row.latitude, longitude=row.longitude)
        lap_input = validity.LapValidityInput(
            lap_id=row.lap_id,
            lap_time_s=row.lap_time_s,
            is_outlier=bool(row.is_outlier),
            outlier_reason=row.outlier_reason,
            excluded_by_user=bool(row.excluded_by_user),
            position=position,
            speed_kmh=row.speed_kmh,
            braking=row.braking,
        )
        result = validity.classify_lap(
            lap_input,
            implausible_floor_s=floors.get((row.track_name, row.engine_category)),
            reference=references.get(row.track_name),
            config=config,
        )
        cur.execute(
            "UPDATE laps SET validity_status = %s, validity_reason = %s, validity_checked_at = now() WHERE id = %s",
            (result.status, result.reason, result.lap_id),
        )
        checked += 1
    conn.commit()
    logger.info("validity-checked %d lap(s)", checked)
    return checked


# ---------------------------------------------------------------------------
# Phase 2/3: field-relative pace scoring
# ---------------------------------------------------------------------------

# The public-eligible predicate a session must clear to participate in a
# cohort at all -- as either the session being rated or a peer another
# driver is compared against. Mirrors track_driver_podium's predicate
# (0011) exactly: a private/unshared session's pace never leaks into
# another driver's rating change, the same way it never appears on a
# public leaderboard.
_PUBLIC_ELIGIBLE_SQL = """
    s.visibility = 'shared' AND s.attribution_status = 'confirmed'
    AND p.claim_status = 'claimed' AND p.user_id IS NOT NULL
"""


def _load_session_paces(conn) -> list[elo.SessionPace]:
    df = pgdb.read_sql(
        conn,
        f"""
        SELECT s.id AS session_db_id, s.driver_profile_id, s.track_name, s.start_date,
               COALESCE(s.track_condition, '') AS track_condition, COALESCE(s.engine_category, '') AS engine_category,
               MIN(l.lap_time_s) AS pace_s
          FROM sessions s
          JOIN laps l ON l.session_db_id = s.id
          JOIN driver_profiles p ON p.id = s.driver_profile_id
         WHERE l.validity_status = 'verified' AND s.track_name IS NOT NULL
           AND {_PUBLIC_ELIGIBLE_SQL}
         GROUP BY s.id, s.driver_profile_id, s.track_name, s.start_date, s.track_condition, s.engine_category
        HAVING MIN(l.lap_time_s) IS NOT NULL
        """,
    )
    paces = []
    for row in df.itertuples():
        session_date = _parse_session_date(row.start_date)
        if session_date is None:
            continue
        paces.append(
            elo.SessionPace(
                session_db_id=row.session_db_id,
                driver_profile_id=row.driver_profile_id,
                pace_s=row.pace_s,
                track_name=row.track_name,
                session_date=session_date,
                conditions=row.track_condition,
                engine_category=row.engine_category,
            )
        )
    return paces


def rebuild_pace_references(conn, config: RatingConfig = DEFAULT_RATING_CONFIG) -> int:
    """Nightly refresh of Part 2's historical-reference fallback, over
    every all-time verified public-eligible session -- not just what's new
    in this run, since the whole point is a robust, slow-moving value."""
    paces = _load_session_paces(conn)
    ratings = _load_driver_ratings(conn, {p.driver_profile_id for p in paces})

    buckets: dict[tuple[str, str, str], list[elo.SessionPace]] = defaultdict(list)
    for p in paces:
        buckets[(p.track_name, p.engine_category, p.conditions)].append(p)

    driver_mus = {pid: state.mu for pid, state in ratings.items()}
    cur = conn.cursor()
    written = 0
    for (track_name, engine_category, conditions), sessions in buckets.items():
        top = sorted(sessions, key=lambda s: s.pace_s)[: config.reference_top_n]
        bucket = elo.compute_reference_bucket(top, driver_mus, config)
        if bucket is None:
            continue
        cur.execute(
            """
            INSERT INTO track_pace_reference (track_name, engine_category, track_condition, reference_lap_s, implied_rating, sample_session_count, computed_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (track_name, engine_category, track_condition) DO UPDATE SET
                reference_lap_s = EXCLUDED.reference_lap_s, implied_rating = EXCLUDED.implied_rating,
                sample_session_count = EXCLUDED.sample_session_count, computed_at = now()
            """,
            (track_name, engine_category, conditions, bucket.reference_lap_s, bucket.implied_rating, bucket.sample_session_count),
        )
        written += 1
    conn.commit()
    logger.info("rebuilt %d pace reference bucket(s)", written)
    return written


def _load_driver_ratings(conn, driver_profile_ids: set[int]) -> dict[int, elo.RatingState]:
    if not driver_profile_ids:
        return {}
    df = pgdb.read_sql(
        conn,
        "SELECT driver_profile_id, mu, sigma_at_last_update FROM driver_ratings WHERE driver_profile_id = ANY(%s)",
        (list(driver_profile_ids),),
    )
    ratings = {row.driver_profile_id: elo.RatingState(mu=row.mu, sigma=row.sigma_at_last_update) for row in df.itertuples()}
    for pid in driver_profile_ids:
        ratings.setdefault(pid, elo.RatingState(mu=1500.0, sigma=350.0))
    return ratings


def _already_rated_sessions(conn) -> set[tuple[int, int]]:
    df = pgdb.read_sql(conn, "SELECT driver_profile_id, session_db_id FROM driver_rating_history")
    return {(row.driver_profile_id, row.session_db_id) for row in df.itertuples()}


def _session_weights(paces: list[elo.SessionPace], config: RatingConfig = DEFAULT_RATING_CONFIG) -> dict[int, float]:
    """Sigma-shrink weight per session_db_id: full weight for a driver's
    first verified session in its week, reduced weight for later ones the
    same week -- computed over the *complete* pace history so the weight
    a session gets doesn't depend on which sessions happen to be new in
    this particular run."""
    by_driver_week: dict[tuple[int, date], list[elo.SessionPace]] = defaultdict(list)
    for p in paces:
        by_driver_week[(p.driver_profile_id, streaks.week_start(p.session_date))].append(p)

    weights: dict[int, float] = {}
    for sessions in by_driver_week.values():
        ordered = sorted(sessions, key=lambda s: (s.session_date, s.session_db_id))
        for i, s in enumerate(ordered):
            weights[s.session_db_id] = streaks.midweek_session_weight(i, config)
    return weights


def run_rating_updates(conn, config: RatingConfig = DEFAULT_RATING_CONFIG) -> int:
    """Phase 4: walk verified sessions chronologically, applying field- or
    reference-based updates per day's cohorts. Must run after
    `run_validity_gate` and `rebuild_pace_references` in the same pass."""
    paces = _load_session_paces(conn)
    already_rated = _already_rated_sessions(conn)
    ratings = _load_driver_ratings(conn, {p.driver_profile_id for p in paces})
    weights = _session_weights(paces, config)

    reference_df = pgdb.read_sql(conn, "SELECT track_name, engine_category, track_condition, reference_lap_s, implied_rating, sample_session_count FROM track_pace_reference")
    reference_buckets = {
        (row.track_name, row.engine_category, row.track_condition): elo.ReferenceBucket(
            track_name=row.track_name, engine_category=row.engine_category, conditions=row.track_condition,
            reference_lap_s=row.reference_lap_s, implied_rating=row.implied_rating, sample_session_count=row.sample_session_count,
        )
        for row in reference_df.itertuples()
    }

    by_date: dict[date, list[elo.SessionPace]] = defaultdict(list)
    for p in paces:
        by_date[p.session_date].append(p)

    updates: list[elo.RatingUpdate] = []
    for session_date in sorted(by_date):
        cohorts = elo.group_into_cohorts(by_date[session_date])
        for key, cohort in cohorts.items():
            track_name, _, conditions, engine_category = key
            unrated_in_cohort = [s for s in cohort if (s.driver_profile_id, s.session_db_id) not in already_rated]
            if not unrated_in_cohort:
                continue

            if len(cohort) - 1 >= config.min_cohort_others:
                # Every driver in `cohort` is compared against current mu
                # (not a point-in-time snapshot for this date) -- a session
                # uploaded late for an old date compares against peers'
                # *current* ratings, a documented simplification rather
                # than reconstructing history. Only the not-yet-rated
                # drivers' updates are kept; an already-rated peer's own
                # recorded change from a prior run is never revised, only
                # used as this cohort's comparison point.
                cohort_updates = elo.field_based_update(cohort, ratings, weights, config)
                new_updates = [u for u in cohort_updates if (u.driver_profile_id, u.session_db_id) not in already_rated]
            else:
                bucket = reference_buckets.get((track_name, engine_category, conditions))
                if bucket is None:
                    continue
                new_updates = [
                    elo.reference_based_update(s, bucket, ratings[s.driver_profile_id], weights.get(s.session_db_id, 1.0), config)
                    for s in unrated_in_cohort
                ]

            # Apply immediately so later cohorts/dates in this same pass
            # see this update's effect on mu/sigma -- Elo is path-dependent.
            for u in new_updates:
                ratings[u.driver_profile_id] = elo.RatingState(mu=u.mu_after, sigma=u.sigma_after)
                already_rated.add((u.driver_profile_id, u.session_db_id))
            updates.extend(new_updates)

    cur = conn.cursor()
    for u in updates:
        cur.execute(
            """
            INSERT INTO driver_rating_history
                (driver_profile_id, session_db_id, mechanism, cohort_track, cohort_date, cohort_conditions,
                 cohort_class, cohort_size, mu_before, mu_after, sigma_before, sigma_after, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (driver_profile_id, session_db_id) DO NOTHING
            """,
            (
                u.driver_profile_id, u.session_db_id, u.mechanism, u.cohort_track, u.cohort_date, u.cohort_conditions,
                u.cohort_class, u.cohort_size, u.mu_before, u.mu_after, u.sigma_before, u.sigma_after, u.note,
            ),
        )
    for driver_profile_id, state in ratings.items():
        touched = any(u.driver_profile_id == driver_profile_id for u in updates)
        if not touched:
            continue
        last_session_date = max(
            (u.cohort_date for u in updates if u.driver_profile_id == driver_profile_id and u.cohort_date), default=None
        )
        n_new = sum(1 for u in updates if u.driver_profile_id == driver_profile_id)
        cur.execute(
            """
            INSERT INTO driver_ratings (driver_profile_id, mu, sigma_at_last_update, last_verified_session_at, sessions_rated_count, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (driver_profile_id) DO UPDATE SET
                mu = EXCLUDED.mu, sigma_at_last_update = EXCLUDED.sigma_at_last_update,
                last_verified_session_at = GREATEST(driver_ratings.last_verified_session_at, EXCLUDED.last_verified_session_at),
                sessions_rated_count = driver_ratings.sessions_rated_count + %s, updated_at = now()
            """,
            (driver_profile_id, state.mu, state.sigma, last_session_date, n_new, n_new),
        )
    conn.commit()
    logger.info("applied %d rating update(s) across %d driver(s)", len(updates), len({u.driver_profile_id for u in updates}))
    return len(updates)


# ---------------------------------------------------------------------------
# Phase 5: weekly activity + streaks
# ---------------------------------------------------------------------------


def rebuild_streaks(conn, config: RatingConfig = DEFAULT_RATING_CONFIG) -> int:
    paces = _load_session_paces(conn)
    by_driver_week: dict[tuple[int, date], int] = defaultdict(int)
    for p in paces:
        by_driver_week[(p.driver_profile_id, streaks.week_start(p.session_date))] += 1

    by_driver: dict[int, list[streaks.WeekCount]] = defaultdict(list)
    for (driver_profile_id, week), count in by_driver_week.items():
        by_driver[driver_profile_id].append(streaks.WeekCount(week_start=week, verified_session_count=count))

    cur = conn.cursor()
    for driver_profile_id, weeks in by_driver.items():
        for w in weeks:
            cur.execute(
                """
                INSERT INTO driver_weekly_activity (driver_profile_id, week_start, verified_session_count, has_midweek_bonus, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (driver_profile_id, week_start) DO UPDATE SET
                    verified_session_count = EXCLUDED.verified_session_count,
                    has_midweek_bonus = EXCLUDED.has_midweek_bonus, updated_at = now()
                """,
                (driver_profile_id, w.week_start, w.verified_session_count, w.verified_session_count >= config.midweek_bonus_min_sessions),
            )
        qualifying = streaks.qualifying_weeks(weeks, config)
        state = streaks.compute_streak(qualifying, config)
        cur.execute(
            """
            INSERT INTO driver_streaks (driver_profile_id, current_streak, longest_streak, freezes_available, last_qualifying_week, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (driver_profile_id) DO UPDATE SET
                current_streak = EXCLUDED.current_streak, longest_streak = GREATEST(driver_streaks.longest_streak, EXCLUDED.longest_streak),
                freezes_available = EXCLUDED.freezes_available, last_qualifying_week = EXCLUDED.last_qualifying_week, updated_at = now()
            """,
            (driver_profile_id, state.current_streak, state.longest_streak, state.freezes_available, state.last_qualifying_week),
        )
    conn.commit()
    logger.info("rebuilt streak state for %d driver(s)", len(by_driver))
    return len(by_driver)


def run_rating_batch(config: RatingConfig = DEFAULT_RATING_CONFIG, recheck_all: bool = False) -> dict[str, int]:
    """The full nightly pass, in the one order that's actually correct --
    see the module docstring."""
    with pgdb.connect() as conn:
        result = {
            "track_reference_lines": rebuild_track_reference_lines(conn, config),
            "laps_validity_checked": run_validity_gate(conn, config, recheck_all=recheck_all),
            "pace_reference_buckets": rebuild_pace_references(conn, config),
            "rating_updates": run_rating_updates(conn, config),
            "drivers_streak_rebuilt": rebuild_streaks(conn, config),
        }
    return result
