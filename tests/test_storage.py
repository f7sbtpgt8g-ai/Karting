import os

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
