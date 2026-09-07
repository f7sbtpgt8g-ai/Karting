"""Inferring a circuit from where the session was driven.

The sync tool cannot know what track it is at, so the name is worked out
from GPS. The bar for that is not "usually right": a session labelled with
the wrong circuit puts lap times next to a reference they have nothing to
do with, and unlike a blank name, nothing about it looks wrong. So these
tests are mostly about the cases where it must decline to answer.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from telemetry.track_naming import (
    MATCH_RADIUS_M,
    haversine_m,
    nearest_track,
    session_location,
)

# Two real Danish kart circuits, far enough apart that nothing should ever
# confuse them, plus a point on the far side of the country.
BARMOSEN = (55.0722, 11.7861)
KORSOR = (55.3336, 11.1389)
AALBORG = (57.0488, 9.9217)


class _FakeSession:
    """Just enough of `Session` for `session_location`: it only ever calls
    `gps_fixes()`."""

    def __init__(self, frame: pd.DataFrame | None, raises: bool = False):
        self._frame = frame
        self._raises = raises

    def gps_fixes(self) -> pd.DataFrame:
        if self._raises:
            raise KeyError("Latitude")
        return self._frame


def _fixes(points: list[tuple[float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {"Latitude": [p[0] for p in points], "Longitude": [p[1] for p in points]}
    )


def _lap_around(centre: tuple[float, float], radius_deg: float = 0.002, n: int = 100):
    """A circular lap around `centre` -- a stand-in for a circuit's shape."""
    angles = np.linspace(0, 2 * np.pi, n)
    return [
        (centre[0] + radius_deg * np.cos(a), centre[1] + radius_deg * np.sin(a)) for a in angles
    ]


# ------------------------------------------------------------------ distance


def test_haversine_matches_a_known_distance():
    # Barmosen to Korsor is about 51 km as the crow flies.
    metres = haversine_m(*BARMOSEN, *KORSOR)
    assert 45_000 < metres < 60_000


def test_haversine_is_symmetric_and_zero_on_itself():
    assert haversine_m(*BARMOSEN, *BARMOSEN) == pytest.approx(0.0, abs=1e-6)
    assert haversine_m(*BARMOSEN, *KORSOR) == pytest.approx(haversine_m(*KORSOR, *BARMOSEN))


def test_longitude_is_corrected_for_latitude():
    """At 55 degrees north a degree of longitude is much shorter than a
    degree of latitude. Treating them alike would make the east-west
    tolerance nearly double the north-south one, so a session could match a
    circuit 3 km to the east but not one 2 km to the north."""
    north = haversine_m(55.0, 12.0, 55.01, 12.0)
    east = haversine_m(55.0, 12.0, 55.0, 12.01)
    assert north > east
    assert east / north == pytest.approx(np.cos(np.radians(55.0)), rel=0.01)


# ------------------------------------------------------------------ location


def test_location_is_the_middle_of_the_lap():
    lat, lon = session_location(_FakeSession(_fixes(_lap_around(BARMOSEN))))
    assert haversine_m(lat, lon, *BARMOSEN) < 100


def test_one_dropped_fix_does_not_move_the_location():
    """A logger with no lock reports (0, 0). A mean would drag the session
    into the Atlantic and match no track at all; the median ignores it."""
    points = _lap_around(BARMOSEN) + [(0.0, 0.0)]
    lat, lon = session_location(_FakeSession(_fixes(points)))
    assert haversine_m(lat, lon, *BARMOSEN) < 100


def test_a_session_with_no_lock_at_all_has_no_location():
    assert session_location(_FakeSession(_fixes([(0.0, 0.0), (0.0, 0.0)]))) is None


def test_a_session_with_no_gps_has_no_location():
    assert session_location(_FakeSession(_fixes([]))) is None
    assert session_location(_FakeSession(pd.DataFrame({"Latitude": [], "Longitude": []}))) is None


def test_a_session_with_no_position_channel_is_not_an_error():
    # An export with no Latitude column at all raises inside `gps_fixes`.
    # That is a session to skip, not a crash mid-ingest.
    assert session_location(_FakeSession(None, raises=True)) is None


def test_missing_values_are_dropped():
    points = _lap_around(BARMOSEN)
    frame = _fixes(points)
    frame.loc[0:10, "Latitude"] = np.nan
    lat, lon = session_location(_FakeSession(frame))
    assert haversine_m(lat, lon, *BARMOSEN) < 200


# ------------------------------------------------------------------- matching


def test_picks_the_track_it_was_driven_at():
    known = [("Barmosen", *BARMOSEN), ("Korsor", *KORSOR)]
    assert nearest_track(BARMOSEN, known) == "Barmosen"
    assert nearest_track(KORSOR, known) == "Korsor"


def test_picks_the_nearest_when_two_are_in_range():
    close = (BARMOSEN[0] + 0.004, BARMOSEN[1])
    known = [("Nearly", *close), ("Barmosen", *BARMOSEN)]
    assert nearest_track(BARMOSEN, known) == "Barmosen"


def test_declines_rather_than_guessing_at_an_unknown_track():
    """The important case. A circuit nobody has named yet must come back
    unnamed -- a blank name is a prompt to fill in, a wrong one is a lap
    time compared against the wrong reference."""
    known = [("Barmosen", *BARMOSEN), ("Korsor", *KORSOR)]
    assert nearest_track(AALBORG, known) is None


def test_declines_when_nothing_is_named_yet():
    assert nearest_track(BARMOSEN, []) is None


def test_matches_across_the_paddock_but_not_across_the_county():
    # Anywhere on a circuit is within the radius of its reference point...
    edge = (BARMOSEN[0] + 0.005, BARMOSEN[1] + 0.005)
    assert nearest_track(edge, [("Barmosen", *BARMOSEN)]) == "Barmosen"
    # ...and a point just past the radius is not.
    far = (BARMOSEN[0] + (MATCH_RADIUS_M * 1.5) / 111_000, BARMOSEN[1])
    assert nearest_track(far, [("Barmosen", *BARMOSEN)]) is None


def test_a_lap_at_a_known_track_names_itself_end_to_end():
    """The whole point: a synced session with nothing but GPS gets the name
    of the circuit it was driven at."""
    known = [("Barmosen", *BARMOSEN), ("Korsor", *KORSOR), ("Aalborg", *AALBORG)]
    point = session_location(_FakeSession(_fixes(_lap_around(KORSOR))))
    assert nearest_track(point, known) == "Korsor"
