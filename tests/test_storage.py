import os

import pandas as pd

from telemetry.setup_config import KartSetup
from telemetry.storage import SessionLibrary


def test_save_and_load_session_roundtrip(tmp_path, session1):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)

    session_db_id = lib.save_session(session1, driver="Test Driver", track_name="Test Track", session_type="practice")
    listed = lib.list_sessions()
    assert len(listed) == 1
    assert listed.iloc[0]["driver"] == "Test Driver"
    assert listed.iloc[0]["n_laps"] > 0

    reloaded = lib.load_session(session_db_id)
    assert len(reloaded.df) == len(session1.df)
    assert reloaded.source_file == session1.source_file

    laps = lib.laps_for_session(session_db_id)
    assert len(laps) == len(session1.df["Lap Number"].unique())
    lib.close()


def test_delete_session(tmp_path, session1, session2):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)

    id1 = lib.save_session(session1, driver="Test Driver", track_name="Test Track")
    id2 = lib.save_session(session2, driver="Test Driver", track_name="Test Track")
    cache_path = lib.list_sessions().set_index("id").loc[id1, "cache_path"]
    assert os.path.exists(cache_path)

    lib.delete_session(id1)

    remaining = lib.list_sessions()
    assert len(remaining) == 1
    assert remaining.iloc[0]["id"] == id2
    assert lib.laps_for_session(id1).empty
    assert not os.path.exists(cache_path)
    lib.close()


def test_progression_across_two_sessions(tmp_path, session1, session2):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)
    lib.save_session(session1, driver="Test Driver", track_name="Test Track")
    lib.save_session(session2, driver="Test Driver", track_name="Test Track")

    listed = lib.list_sessions()
    assert len(listed) == 2
    lib.close()


def test_find_session_dedupe(tmp_path, session1):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)

    assert lib.find_session(session1.source_file, session1.session_id, session1.start_time) is None
    session_db_id = lib.save_session(session1)
    found_id = lib.find_session(session1.source_file, session1.session_id, session1.start_time)
    assert found_id == session_db_id
    lib.close()


def test_kart_setup_history_roundtrip(tmp_path):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)

    session_key = ("session1.tsv", 0, "10:00:00")
    assert lib.load_latest_kart_setup_for_session(*session_key) is None

    setup1 = KartSetup(driver="Test Driver")
    setup1.gearing.rear_teeth = 78
    lib.save_kart_setup(setup1, *session_key, driver="Test Driver")

    setup2 = KartSetup(driver="Test Driver")
    setup2.gearing.rear_teeth = 80
    setup_id_2 = lib.save_kart_setup(setup2, *session_key, driver="Test Driver")

    history = lib.list_kart_setups()
    assert len(history) == 2

    latest = lib.load_latest_kart_setup_for_session(*session_key)
    assert latest.gearing.rear_teeth == 80

    reloaded = lib.load_kart_setup(setup_id_2)
    assert reloaded.gearing.rear_teeth == 80
    lib.close()


def test_log_and_query_corner_metrics(tmp_path, session1):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)
    session_db_id = lib.save_session(session1, driver="Test Driver", track_name="Test Track")

    corner_points = pd.DataFrame(
        [
            {
                "corner_label": "Corner 1", "entry_distance_m": 50.0, "entry_speed_kmh": 120.0,
                "apex_distance_m": 80.0, "apex_speed_kmh": 50.0, "exit_distance_m": 95.0, "exit_speed_kmh": 70.0,
            }
        ]
    )
    zone_times = pd.DataFrame(
        [{"corner_label": "Corner 1", "zone_a_time_s": 1.2, "zone_b_time_s": 2.0, "zone_c_time_s": 3.5}]
    )
    lib.log_corner_metrics(session_db_id, "Test Driver", "Test Track", 1, corner_points, zone_times)

    with lib._connect() as conn:
        logged = pd.read_sql_query("SELECT * FROM corner_metrics", conn)
    assert len(logged) == 1
    assert logged.iloc[0]["corner_label"] == "Corner 1"
    assert logged.iloc[0]["apex_speed_kmh"] == 50.0
    lib.close()


def test_log_corner_metrics_is_noop_on_empty(tmp_path, session1):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)
    session_db_id = lib.save_session(session1, driver="Test Driver", track_name="Test Track")
    lib.log_corner_metrics(session_db_id, "Test Driver", "Test Track", 1, pd.DataFrame(), pd.DataFrame())
    with lib._connect() as conn:
        logged = pd.read_sql_query("SELECT * FROM corner_metrics", conn)
    assert logged.empty
    lib.close()


def test_log_pattern_instances_and_recurring_summary(tmp_path, session1, session2):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)
    id1 = lib.save_session(session1, driver="Test Driver", track_name="Test Track")
    id2 = lib.save_session(session2, driver="Test Driver", track_name="Test Track")

    comparisons = pd.DataFrame(
        [
            {
                "corner_label": "Corner 1", "pattern_type": "compromised_exit_fast_entry", "confidence": "medium",
                "net_time_impact_s": 0.25, "evidence": {"entry_speed_delta_kmh": 5.0},
            },
            {
                "corner_label": "Corner 2", "pattern_type": "clean_no_significant_delta", "confidence": "high",
                "net_time_impact_s": 0.0, "evidence": {},
            },
        ]
    )
    lib.log_pattern_instances("Test Driver", "Test Track", id1, 5, id2, 3, comparisons)
    # Same pattern shows up again in a second session -> should count as recurring.
    lib.log_pattern_instances("Test Driver", "Test Track", id2, 4, id2, 3, comparisons)

    history = lib.pattern_instance_history(driver="Test Driver", corner_label="Corner 1")
    assert len(history) == 2
    assert history.iloc[0]["pattern_type"] == "compromised_exit_fast_entry"

    summary = lib.recurring_pattern_summary(driver="Test Driver", min_occurrences=2)
    assert len(summary) == 1  # only Corner 1's pattern recurs across 2 distinct sessions
    row = summary.iloc[0]
    assert row["corner_label"] == "Corner 1"
    assert row["pattern_type"] == "compromised_exit_fast_entry"
    assert row["n_sessions"] == 2
    lib.close()


def test_kart_setup_is_scoped_per_session(tmp_path):
    db_path = os.path.join(tmp_path, "sessions.db")
    lib = SessionLibrary(db_path)

    session_a = ("session1.tsv", 0, "10:00:00")
    session_b = ("session1.tsv", 1, "11:15:00")

    setup_a = KartSetup(driver="Test Driver")
    setup_a.gearing.rear_teeth = 78
    lib.save_kart_setup(setup_a, *session_a, driver="Test Driver")

    # Session B has never had a setup saved -- must NOT inherit session A's.
    assert lib.load_latest_kart_setup_for_session(*session_b) is None
    assert lib.load_latest_kart_setup_for_session(*session_a).gearing.rear_teeth == 78
    lib.close()
