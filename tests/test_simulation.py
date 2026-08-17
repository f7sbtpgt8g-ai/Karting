from telemetry.laps import clean_lap_table, flag_outlier_laps, lap_table
from telemetry.setup_config import KartSetup
from telemetry.simulation import (
    build_accel_rpm_curve,
    estimate_lap_time_delta,
    fit_speed_rpm_scale,
    simulate_gearing_change,
)


def _clean_laps(session1):
    laps = flag_outlier_laps(lap_table(session1))
    clean = clean_lap_table(laps)
    return clean["lap_number"].tolist(), int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])


def test_fit_speed_rpm_scale_returns_plausible_value(session1):
    clean_lap_numbers, _ = _clean_laps(session1)
    scale = fit_speed_rpm_scale(session1, clean_lap_numbers)
    assert scale is not None
    # Fixture generates rpm ~= 400 + speed_kmh * 131, so RPM/speed should
    # land somewhere in a broad, physically sane band -- not exact, since
    # the fit is a plain ratio without the fixture's additive offset.
    assert 50 < scale < 500


def test_build_accel_rpm_curve_has_expected_shape(session1):
    clean_lap_numbers, _ = _clean_laps(session1)
    curve = build_accel_rpm_curve(session1, clean_lap_numbers)
    assert not curve.empty
    assert list(curve.columns) == ["rpm_bin_center", "accel_g", "n_samples"]
    assert (curve["rpm_bin_center"].diff().dropna() > 0).all()  # sorted ascending


def test_simulate_gearing_change_with_no_ratio_change_tracks_actual_speed(session1):
    clean_lap_numbers, best_lap = _clean_laps(session1)
    setup = KartSetup()
    setup.gearing.front_teeth = 10
    setup.gearing.rear_teeth = 80
    scale = fit_speed_rpm_scale(session1, clean_lap_numbers)
    curve = build_accel_rpm_curve(session1, clean_lap_numbers)

    sim = simulate_gearing_change(session1, best_lap, setup, rear_teeth_delta=0, front_teeth_delta=0, speed_rpm_scale=scale, accel_curve=curve)
    assert not sim.empty
    assert {"distance_m", "speed_kmh_actual", "speed_kmh_sim", "rpm_actual", "rpm_sim"}.issubset(sim.columns)
    # Zero ratio change should stay reasonably close to the actual trace
    # (not identical, since the accel-vs-RPM curve is a lookup, not a
    # perfect replay).
    diff = (sim["speed_kmh_sim"] - sim["speed_kmh_actual"]).abs()
    assert diff.median() < 15


def test_simulate_gearing_change_extra_rear_tooth_raises_simulated_rpm(session1):
    clean_lap_numbers, best_lap = _clean_laps(session1)
    setup = KartSetup()
    setup.gearing.front_teeth = 10
    setup.gearing.rear_teeth = 80
    scale = fit_speed_rpm_scale(session1, clean_lap_numbers)
    curve = build_accel_rpm_curve(session1, clean_lap_numbers)

    sim = simulate_gearing_change(session1, best_lap, setup, rear_teeth_delta=1, front_teeth_delta=0, speed_rpm_scale=scale, accel_curve=curve)
    # Adding a rear tooth raises the ratio, so RPM at any given simulated
    # speed must be higher than the actual RPM was at that speed -- check
    # via the scale directly rather than a noisy point-by-point compare.
    ratio_scale = (81 / 10) / (80 / 10)
    assert ratio_scale > 1
    implied_rpm = sim["speed_kmh_sim"] * scale * ratio_scale
    assert (sim["rpm_sim"] == implied_rpm).all()


def test_estimate_lap_time_delta_zero_for_identical_traces():
    import pandas as pd

    sim_trace = pd.DataFrame(
        {
            "distance_m": [0, 10, 20, 30],
            "speed_kmh_actual": [50, 60, 70, 80],
            "speed_kmh_sim": [50, 60, 70, 80],
        }
    )
    result = estimate_lap_time_delta(sim_trace, actual_lap_time_s=30.0)
    assert abs(result["delta_s"]) < 1e-9
    assert abs(result["sim_lap_time_s"] - 30.0) < 1e-9


def test_estimate_lap_time_delta_negative_when_simulated_is_faster():
    import pandas as pd

    sim_trace = pd.DataFrame(
        {
            "distance_m": [0, 10, 20, 30],
            "speed_kmh_actual": [50, 50, 50, 50],
            "speed_kmh_sim": [60, 60, 60, 60],
        }
    )
    result = estimate_lap_time_delta(sim_trace, actual_lap_time_s=30.0)
    assert result["delta_s"] < 0
    assert result["sim_lap_time_s"] < 30.0
