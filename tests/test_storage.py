import os

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
