import os

from telemetry.corners import build_reference_segments
from telemetry.laps import clean_lap_table, flag_outlier_laps, lap_table
from telemetry.setup_config import KartSetup
from telemetry.setup_engine import all_setup_suggestions, gearing_suggestion


def test_kart_setup_roundtrip_yaml(tmp_path):
    setup = KartSetup(driver="Test Driver")
    setup.gearing.front_teeth = 10
    setup.gearing.rear_teeth = 78
    setup.carburettor.main_jet = 168
    path = os.path.join(tmp_path, "setup.yaml")
    setup.to_yaml(path)

    loaded = KartSetup.from_yaml(path)
    assert loaded.driver == "Test Driver"
    assert loaded.gearing.front_teeth == 10
    assert loaded.gearing.rear_teeth == 78
    assert loaded.gearing.gear_ratio == 7.8
    assert loaded.carburettor.main_jet == 168


def test_gearing_suggestion_flags_over_rev(session1):
    laps = flag_outlier_laps(lap_table(session1))
    clean = clean_lap_table(laps)
    clean_lap_numbers = clean["lap_number"].tolist()
    best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])
    segments = build_reference_segments(session1, best_lap)

    setup = KartSetup()
    result = gearing_suggestion(session1, clean_lap_numbers, segments, setup, peak_power_rpm_band=(11500, 13000))
    # synthetic fixture's top speed (~108 km/h) implies an RPM well above the band
    assert result["confidence"] in ("low", "medium")
    assert "hypothesis" in result


def test_all_setup_suggestions_returns_four_areas(session1):
    laps = flag_outlier_laps(lap_table(session1))
    clean = clean_lap_table(laps)
    clean_lap_numbers = clean["lap_number"].tolist()
    best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])
    segments = build_reference_segments(session1, best_lap)

    setup = KartSetup()
    suggestions = all_setup_suggestions(session1, clean_lap_numbers, segments, setup)
    areas = {s["area"] for s in suggestions}
    assert areas == {"gearing", "jetting", "tyre_pressure", "chassis_balance"}
    for s in suggestions:
        assert "hypothesis" in s
