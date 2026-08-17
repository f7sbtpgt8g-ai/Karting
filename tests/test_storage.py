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

    assert lib.load_latest_kart_setup() is None

    setup1 = KartSetup(driver="Test Driver")
    setup1.gearing.rear_teeth = 78
    lib.save_kart_setup(setup1, driver="Test Driver")

    setup2 = KartSetup(driver="Test Driver")
    setup2.gearing.rear_teeth = 80
    setup_id_2 = lib.save_kart_setup(setup2, driver="Test Driver")

    history = lib.list_kart_setups()
    assert len(history) == 2

    latest = lib.load_latest_kart_setup()
    assert latest.gearing.rear_teeth == 80

    reloaded = lib.load_kart_setup(setup_id_2)
    assert reloaded.gearing.rear_teeth == 80
    lib.close()
