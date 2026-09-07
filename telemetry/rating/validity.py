"""Part 1: the validity gate every lap passes through before anything in
Part 2 (field-relative pace scoring) can see it.

Deliberately reads from what's already persisted and queryable -- the
`lap_traces` arrays (distance_m/latitude/longitude/speed_kmh/braking) that
back the web Lap Analysis page -- rather than re-loading a session's raw
parsed dataframe. That keeps this independent of whether a session's
Parquet blob still exists (many don't, once `scripts/backfill_analysis.py
--clear-blobs` has run) and reuses the same braking inference
(`add_braking_throttle_estimates`, applied once at ingest time and stored
as `lap_traces.braking`) rather than recomputing it.

Every function here is pure and takes plain data in -- no database access --
so it can be unit tested against synthetic traces exactly the way
`test_corner_causal.py` tests `corner_causal.py`. `telemetry/rating/engine.py`
is the only thing that queries Postgres and feeds these functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .config import DEFAULT_RATING_CONFIG, RatingConfig

VERIFIED = "verified"
FLAGGED = "flagged"
EXCLUDED = "excluded"

# Local equirectangular projection scale -- metres per degree of latitude is
# constant everywhere; metres per degree of longitude shrinks with
# cos(latitude). Good to well under a metre of error over a kart track's
# few-hundred-metre footprint, which is what track-cut tolerances are
# measured in anyway.
_METERS_PER_DEGREE_LAT = 111_320.0


@dataclass
class LapPositionTrace:
    """One lap's GPS path, in the shape `lap_traces` stores it: parallel
    arrays, distance_m monotonically increasing from 0 within the lap."""

    distance_m: Sequence[float]
    latitude: Sequence[float]
    longitude: Sequence[float]


@dataclass
class ReferenceLine:
    """A track's consensus racing-line corridor: median position and
    per-point tolerance, resampled to a fixed number of points by arc-length
    fraction (not raw distance -- lap lengths vary lap to lap)."""

    lat: np.ndarray
    lon: np.ndarray
    tolerance_m: np.ndarray
    built_from_lap_count: int


@dataclass
class TrackCutCheck:
    is_cut: bool
    max_deviation_m: float
    offending_points: int


@dataclass
class LapValidityInput:
    lap_id: int
    lap_time_s: float | None
    is_outlier: bool
    outlier_reason: str | None
    excluded_by_user: bool
    position: LapPositionTrace | None = None
    speed_kmh: Sequence[float | None] | None = None
    braking: Sequence[bool | None] | None = None


@dataclass
class LapValidityResult:
    lap_id: int
    status: str
    reason: str | None


def _resample_by_fraction(
    distance_m: Sequence[float], values: Sequence[float], n_points: int
) -> np.ndarray:
    """`values` (e.g. latitude) resampled onto `n_points` uniform arc-length
    fractions of the lap (0..1), by linear interpolation against distance."""
    distance = np.asarray(distance_m, dtype=float)
    vals = np.asarray(values, dtype=float)
    total = distance[-1] - distance[0]
    if total <= 0:
        return np.full(n_points, np.nan)
    fractions = np.linspace(0.0, 1.0, n_points)
    targets = distance[0] + fractions * total
    return np.interp(targets, distance, vals)


def _planar_distance_m(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Flat-earth distance between two equal-length arrays of lat/lon
    points, in metres. See the module-level projection-scale comment."""
    lat0 = np.nanmean(np.concatenate([lat1, lat2]))
    m_per_deg_lon = _METERS_PER_DEGREE_LAT * np.cos(np.radians(lat0))
    dy = (lat2 - lat1) * _METERS_PER_DEGREE_LAT
    dx = (lon2 - lon1) * m_per_deg_lon
    return np.sqrt(dx**2 + dy**2)


def build_reference_line(
    traces: Sequence[LapPositionTrace], config: RatingConfig = DEFAULT_RATING_CONFIG
) -> ReferenceLine | None:
    """The consensus line for one track, from many verified laps' GPS paths.

    Per-point tolerance is `max(floor, k * MAD)` at that point across the
    laps that built the line -- a floor because GPS jitter alone is a few
    metres even on a lap that cut nothing straight down the middle, and a
    MAD term (not stdev) so one contributing lap with its own bad fix at one
    point doesn't blow out the tolerance for every other point.

    Returns None if there aren't enough laps to call the result a
    consensus rather than one or two lucky/unlucky traces.
    """
    if len(traces) < config.reference_line_min_laps:
        return None

    n = config.reference_line_points
    lats = np.stack([_resample_by_fraction(t.distance_m, t.latitude, n) for t in traces])
    lons = np.stack([_resample_by_fraction(t.distance_m, t.longitude, n) for t in traces])

    ref_lat = np.nanmedian(lats, axis=0)
    ref_lon = np.nanmedian(lons, axis=0)

    deviations = _planar_distance_m(
        lats, lons, np.broadcast_to(ref_lat, lats.shape), np.broadcast_to(ref_lon, lons.shape)
    )
    mad = np.nanmedian(np.abs(deviations - np.nanmedian(deviations, axis=0)), axis=0)
    tolerance = np.maximum(config.reference_line_tolerance_floor_m, config.reference_line_tolerance_k * mad)

    return ReferenceLine(lat=ref_lat, lon=ref_lon, tolerance_m=tolerance, built_from_lap_count=len(traces))


def track_cut_deviation(
    trace: LapPositionTrace, reference: ReferenceLine, config: RatingConfig = DEFAULT_RATING_CONFIG
) -> TrackCutCheck:
    """How far this lap's path strays from the reference corridor.

    Flags only once at least `track_cut_min_offending_points` resampled
    points fall outside their own point's tolerance -- a single point is
    more likely a stray GPS fix than a genuine cut through a corner, which
    displaces a real run of consecutive points.
    """
    n = len(reference.lat)
    lat = _resample_by_fraction(trace.distance_m, trace.latitude, n)
    lon = _resample_by_fraction(trace.distance_m, trace.longitude, n)
    deviation = _planar_distance_m(lat, lon, reference.lat, reference.lon)
    offending = deviation > reference.tolerance_m
    n_offending = int(np.nansum(offending))
    max_dev = float(np.nanmax(deviation)) if len(deviation) else 0.0
    return TrackCutCheck(
        is_cut=n_offending >= config.track_cut_min_offending_points,
        max_deviation_m=max_dev,
        offending_points=n_offending,
    )


def implausible_floor(
    clean_lap_times_s: Sequence[float], config: RatingConfig = DEFAULT_RATING_CONFIG
) -> float | None:
    """The physically-implausible floor for one track+class bucket, or None
    if there isn't yet enough clean-lap history to compute one responsibly.

    `clean_lap_times_s` is expected to already exclude `is_outlier` laps --
    the statistical-outlier/in-lap/out-lap detector already in `laps.py` is
    a different, tighter signal than "this is not a physically possible lap
    time", and this floor is deliberately looser (0.75x median by default)
    than that one so it only catches the genuinely implausible, not merely
    slow.
    """
    times = [t for t in clean_lap_times_s if t is not None]
    if len(times) < config.implausible_floor_min_sample:
        return None
    return float(np.median(times)) * config.implausible_floor_factor


def speed_drop_without_braking(
    speed_kmh: Sequence[float | None],
    braking: Sequence[bool | None],
    config: RatingConfig = DEFAULT_RATING_CONFIG,
) -> bool:
    """A sudden speed drop with no recorded braking event -- the
    sensor-glitch/spin/off-track filter, reusing the same braking inference
    (`metrics.add_braking_throttle_estimates`) already stored per lap rather
    than a new heuristic. A genuine braking zone always sets
    `braking_estimate` on the samples where speed is actually falling; a
    drop that arrives with braking still False at every one of those
    samples is a GPS/speed glitch, a spin, or an off, not normal driving.
    """
    speed = np.array([s if s is not None else np.nan for s in speed_kmh], dtype=float)
    brake = np.array([bool(b) for b in braking], dtype=bool) if len(braking) == len(speed) else np.zeros_like(speed, dtype=bool)
    if len(speed) < 2:
        return False
    drop = speed[:-1] - speed[1:]
    unbraked_drop = drop > config.speed_drop_threshold_kmh
    unbraked_drop &= ~brake[1:]
    return bool(np.any(unbraked_drop))


def classify_lap(
    lap: LapValidityInput,
    *,
    implausible_floor_s: float | None = None,
    reference: ReferenceLine | None = None,
    config: RatingConfig = DEFAULT_RATING_CONFIG,
) -> LapValidityResult:
    """One lap's verified/flagged/excluded status, and why.

    Order matters: the pre-existing outlier/exclusion signals (in/out laps,
    statistical outliers, a driver's own exclusion) are hard excludes,
    consistent with how they already drop out of best/average stats
    elsewhere in the app. Everything past that is "flagged", never
    auto-excluded -- an implausible time or a possible track cut is held
    for review, not trusted or silently dropped, per the brief.
    """
    if lap.is_outlier:
        reason = f"pre-existing outlier ({lap.outlier_reason})" if lap.outlier_reason else "pre-existing outlier"
        return LapValidityResult(lap.lap_id, EXCLUDED, reason)
    if lap.excluded_by_user:
        return LapValidityResult(lap.lap_id, EXCLUDED, "excluded by driver")

    reasons: list[str] = []

    if lap.lap_time_s is not None and implausible_floor_s is not None and lap.lap_time_s < implausible_floor_s:
        reasons.append(f"implausible_time(<{implausible_floor_s:.1f}s)")

    if lap.position is not None and reference is not None:
        check = track_cut_deviation(lap.position, reference, config)
        if check.is_cut:
            reasons.append(f"possible_track_cut(max_dev={check.max_deviation_m:.1f}m)")

    if lap.speed_kmh is not None and lap.braking is not None:
        if speed_drop_without_braking(lap.speed_kmh, lap.braking, config):
            reasons.append("speed_drop_without_braking")

    if reasons:
        return LapValidityResult(lap.lap_id, FLAGGED, "; ".join(reasons))
    return LapValidityResult(lap.lap_id, VERIFIED, None)
