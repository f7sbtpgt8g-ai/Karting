"""Working out which circuit a session was driven at, from where it was.

The sync tool reads a laptimer, not a calendar: nothing in a `.uni` file
says "Barmosen", so every session it ingests arrives with no track name.
Naming thirty of them by hand after each track day is not a thing anyone
does twice.

But the session does know where it was, to a few metres, on every GPS fix.
So the track name is inferred: take the middle of the lap, compare it with
the tracks already named in this database, and take the closest one within
a couple of kilometres.

That makes the database teach itself. Name Barmosen once -- on one session,
by hand -- and every future session driven there names itself. A circuit
nobody has named yet stays NULL, which is the honest answer and is exactly
what the bulk-rename on Home is for.

Deliberately not a hardcoded list of circuits: this has to work for a club
track in Jutland that appears in no public database, and a wrong name from
a stale gazetteer would be worse than no name at all.
"""

from __future__ import annotations

import logging
import math

from . import db as pgdb
from .parser import Session

logger = logging.getLogger(__name__)

# How close a session has to be to a known track to be called that track.
#
# A kart circuit is roughly a kilometre of tarmac inside a field, so the
# middle of one lap is within a few hundred metres of anywhere on it. Two
# kilometres is comfortably more than that and comfortably less than the
# distance to the next circuit -- the nearest pair of Danish kart tracks is
# tens of kilometres apart.
MATCH_RADIUS_M = 2000.0

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres.

    Haversine rather than a flat approximation: this is compared against a
    fixed radius, and at 56 degrees north a degree of longitude is only 62%
    of a degree of latitude, so treating the two as interchangeable would
    make the east-west tolerance nearly double the north-south one.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def session_location(session: Session) -> tuple[float, float] | None:
    """One representative position for a session, or None with no GPS.

    The median, not the mean: a dropped fix reading (0, 0) off the coast of
    Africa would drag a mean into the Atlantic, and one bad fix is a normal
    thing for a logger to produce. The median ignores it entirely.
    """
    try:
        fixes = session.gps_fixes()
    except Exception:  # noqa: BLE001 - a session with no position channel at all
        return None
    if fixes is None or len(fixes) == 0:
        return None
    if "Latitude" not in fixes or "Longitude" not in fixes:
        return None

    lat = fixes["Latitude"].dropna()
    lon = fixes["Longitude"].dropna()
    if lat.empty or lon.empty:
        return None

    # A fix at exactly (0, 0) is the logger saying "no lock", not a session
    # in the Gulf of Guinea.
    valid = (lat != 0) & (lon != 0)
    lat, lon = lat[valid], lon[valid]
    if lat.empty or lon.empty:
        return None

    return float(lat.median()), float(lon.median())


def nearest_track(
    point: tuple[float, float],
    known: list[tuple[str, float, float]],
    radius_m: float = MATCH_RADIUS_M,
) -> str | None:
    """The closest known track to `point`, or None if none is close enough.

    Returning None rather than the least-bad match is the whole point: an
    unnamed session is a prompt to name it, whereas a session labelled with
    the wrong circuit is a lap time compared against the wrong reference.
    """
    lat, lon = point
    best: tuple[float, str] | None = None
    for name, track_lat, track_lon in known:
        distance = haversine_m(lat, lon, track_lat, track_lon)
        if distance > radius_m:
            continue
        if best is None or distance < best[0]:
            best = (distance, name)
    return best[1] if best else None


def known_track_locations() -> list[tuple[str, float, float]]:
    """One position per already-named track, from the sessions stored here.

    Reads the first GPS fix of any one lap of any one session per track --
    enough to place a circuit to within its own length, which is all the
    match needs.
    """
    if not pgdb.has_postgres_configured():
        return []
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (s.track_name)
                   s.track_name AS track_name,
                   t.latitude[1] AS lat,
                   t.longitude[1] AS lon
              FROM sessions s
              JOIN lap_traces t ON t.session_db_id = s.id
             WHERE s.track_name IS NOT NULL
               AND btrim(s.track_name) <> ''
               AND t.latitude IS NOT NULL
               AND array_length(t.latitude, 1) > 0
               AND t.latitude[1] IS NOT NULL
               AND t.longitude[1] IS NOT NULL
             ORDER BY s.track_name, s.id DESC
            """
        )
        # `telemetry.db.connect` uses a RealDictCursor, so rows are dicts.
        return [
            (row["track_name"], float(row["lat"]), float(row["lon"]))
            for row in cur.fetchall()
        ]


def name_track_if_unknown(session_db_id: int, session: Session) -> str | None:
    """Fill in a session's track name from its position, if it has none.

    Returns the name applied, or None if the session already had one, has no
    usable GPS, or is not near any track named so far.

    Only ever fills a blank: a name the uploader typed is never overwritten,
    however far it is from where the GPS says they were. They were there and
    this function was not.
    """
    if not pgdb.has_postgres_configured():
        return None

    try:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT track_name FROM sessions WHERE id = %s", (session_db_id,))
            row = cur.fetchone()
            if row is None:
                return None
            existing = row["track_name"]
            if existing is not None and str(existing).strip():
                return None

            point = session_location(session)
            if point is None:
                return None

            # Read the known tracks on this same connection, so a session
            # ingested moments ago in the same batch is already available as
            # a reference.
            cur.execute(
                """
                SELECT DISTINCT ON (s.track_name)
                       s.track_name AS track_name,
                       t.latitude[1] AS lat,
                       t.longitude[1] AS lon
                  FROM sessions s
                  JOIN lap_traces t ON t.session_db_id = s.id
                 WHERE s.track_name IS NOT NULL
                   AND btrim(s.track_name) <> ''
                   AND t.latitude IS NOT NULL
                   AND array_length(t.latitude, 1) > 0
                   AND t.latitude[1] IS NOT NULL
                   AND t.longitude[1] IS NOT NULL
                 ORDER BY s.track_name, s.id DESC
                """
            )
            known = [
                (r["track_name"], float(r["lat"]), float(r["lon"])) for r in cur.fetchall()
            ]

            name = nearest_track(point, known)
            if name is None:
                logger.info(
                    "session %s at (%.5f, %.5f) matched no known track", session_db_id, *point
                )
                return None

            cur.execute(
                "UPDATE sessions SET track_name = %s WHERE id = %s AND track_name IS NULL",
                (name, session_db_id),
            )
            conn.commit()
            logger.info("named session %s '%s' from its GPS position", session_db_id, name)
            return name
    except Exception:  # noqa: BLE001 - a convenience, never a reason to fail an ingest
        logger.warning("could not infer a track name for session %s", session_db_id, exc_info=True)
        return None
