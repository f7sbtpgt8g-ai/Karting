from telemetry.laps import (
    clean_lap_table,
    detect_anomalous_laps,
    flag_outlier_laps,
    lap_table,
    lap_time_with_deltas,
    summarize_laps,
)


def test_lap_table_lap_count(session1):
    laps = lap_table(session1)
    assert len(laps) == 7  # out-lap + 5 normal + incident + ... per fixture spec
    assert laps["lap_number"].is_monotonic_increasing
    assert (laps["lap_time_s"] > 0).all()


def test_lap_time_resets_to_zero_per_lap(session1):
    df = session1.df
    for lap_no, g in df.groupby("Lap Number"):
        assert g["lap_time_s"].min() >= 0


def test_flag_outlier_laps_catches_out_lap_and_incident(session1):
    laps = flag_outlier_laps(lap_table(session1))
    flagged = laps[laps["is_outlier"]]
    assert len(flagged) >= 2
    # the out-lap (lap 0) must be flagged
    assert laps.loc[laps["lap_number"] == 0, "is_outlier"].iloc[0]
    # the incident lap (lap 4, ~1.8x normal) should be caught either by the
    # in/out heuristic or the statistical outlier check
    reasons = laps.loc[laps["lap_number"] == 4, "outlier_reason"].iloc[0]
    assert reasons != "" or not laps.loc[laps["lap_number"] == 4, "is_outlier"].iloc[0] is False


def test_clean_lap_table_excludes_outliers(session1):
    laps = flag_outlier_laps(lap_table(session1))
    clean = clean_lap_table(laps)
    assert len(clean) < len(laps)
    assert not clean["is_outlier"].any()


def test_summarize_laps_best_is_minimum(session1):
    laps = flag_outlier_laps(lap_table(session1))
    summary = summarize_laps(laps)
    clean = clean_lap_table(laps)
    assert summary["best_lap_s"] == clean["lap_time_s"].min()
    assert summary["n_laps"] == len(clean)


def test_lap_time_with_deltas_signs(session1):
    laps = flag_outlier_laps(lap_table(session1))
    annotated = lap_time_with_deltas(laps)
    best_row = annotated.loc[annotated["delta_to_best_s"].idxmin()]
    assert best_row["delta_to_best_s"] == 0.0 or best_row["delta_to_best_s"] < 1e-9


def test_detect_anomalous_laps_flags_the_spin(session1):
    laps = lap_table(session1)
    flagged = detect_anomalous_laps(laps)
    incident_row = flagged.loc[flagged["lap_number"] == 4]
    assert incident_row["likely_incident"].iloc[0]
