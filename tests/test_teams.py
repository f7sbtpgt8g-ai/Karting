"""Tests for the team concept in the accounts data layer: creating a team,
the join-request/accept/reject flow, role management, and -- carrying the
most weight, in the same spirit as test_accounts.py's visibility tests --
that 'team' visibility only ever reaches an active teammate, never anyone
else, and never bypasses the existing claimed-profile hard gate.
"""

import os

import pandas as pd
import pytest

from telemetry.accounts import (
    TEAM_ROLE_ADMIN,
    TEAM_ROLE_MANAGER,
    TEAM_ROLE_MEMBER,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    VISIBILITY_TEAM,
    AccountLibrary,
)
from telemetry.storage import SessionLibrary


@pytest.fixture
def libs(tmp_path):
    db_path = os.path.join(tmp_path, "sessions.db")
    sessions = SessionLibrary(db_path)
    accounts = AccountLibrary(db_path)
    return sessions, accounts


def _save(sessions_lib, session, **kwargs):
    return sessions_lib.save_session(session, **kwargs)


# -------------------------------------------------------------- creation


def test_create_team_makes_creator_manager(libs):
    _, accounts = libs
    user_id, profile_id = accounts.register_user_with_profile("manager@example.com")
    team_id = accounts.create_team("Fast Racers", user_id)

    roster = accounts.team_roster(team_id)
    assert len(roster) == 1
    assert roster.iloc[0]["driver_profile_id"] == profile_id
    assert roster.iloc[0]["role"] == TEAM_ROLE_MANAGER

    membership = accounts.get_active_membership_for_profile(profile_id)
    assert membership["team_id"] == team_id
    assert membership["role"] == TEAM_ROLE_MANAGER


def test_cannot_create_a_second_team_while_active_on_one(libs):
    _, accounts = libs
    user_id, _ = accounts.register_user_with_profile("manager@example.com")
    accounts.create_team("Team A", user_id)

    with pytest.raises(ValueError):
        accounts.create_team("Team B", user_id)


# ------------------------------------------------------------- join flow


def test_join_request_is_pending_until_resolved(libs):
    _, accounts = libs
    manager_id, _ = accounts.register_user_with_profile("manager@example.com")
    member_id, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)

    mid = accounts.request_to_join_team(team_id, member_profile)
    assert accounts.get_active_membership_for_profile(member_profile) is None
    pending = accounts.pending_join_requests_for_team(team_id)
    assert len(pending) == 1
    assert pending.iloc[0]["driver_profile_id"] == member_profile

    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager_id)
    membership = accounts.get_active_membership_for_profile(member_profile)
    assert membership is not None
    assert membership["role"] == TEAM_ROLE_MEMBER
    assert accounts.pending_join_requests_for_team(team_id).empty


def test_rejected_join_request_leaves_profile_unaffiliated(libs):
    _, accounts = libs
    manager_id, _ = accounts.register_user_with_profile("manager@example.com")
    _, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)

    mid = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid, accept=False, decided_by_user_id=manager_id)

    assert accounts.get_active_membership_for_profile(member_profile) is None
    # A rejected request doesn't block trying again elsewhere.
    membership = accounts.get_membership_for_profile(member_profile)
    assert membership["status"] == "rejected"


def test_duplicate_join_request_is_blocked(libs):
    _, accounts = libs
    manager_id, _ = accounts.register_user_with_profile("manager@example.com")
    _, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)

    accounts.request_to_join_team(team_id, member_profile)
    with pytest.raises(ValueError):
        accounts.request_to_join_team(team_id, member_profile)


def test_cannot_join_two_teams_at_once(libs):
    _, accounts = libs
    manager1, _ = accounts.register_user_with_profile("manager1@example.com")
    manager2, _ = accounts.register_user_with_profile("manager2@example.com")
    _, member_profile = accounts.register_user_with_profile("member@example.com")
    team1 = accounts.create_team("Team One", manager1)
    team2 = accounts.create_team("Team Two", manager2)

    mid = accounts.request_to_join_team(team1, member_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager1)

    with pytest.raises(ValueError):
        accounts.request_to_join_team(team2, member_profile)


# --------------------------------------------------------------- roles


def test_role_management_promote_demote_transfer(libs):
    _, accounts = libs
    manager_id, manager_profile = accounts.register_user_with_profile("manager@example.com")
    _, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)
    mid = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager_id)

    accounts.set_member_role(mid, TEAM_ROLE_ADMIN)
    assert accounts.get_active_membership_for_profile(member_profile)["role"] == TEAM_ROLE_ADMIN

    # The manager role can't be changed via set_member_role.
    manager_membership = accounts.get_active_membership_for_profile(manager_profile)
    with pytest.raises(ValueError):
        accounts.set_member_role(int(manager_membership["id"]), TEAM_ROLE_MEMBER)

    accounts.transfer_team_manager(team_id, mid)
    assert accounts.get_active_membership_for_profile(member_profile)["role"] == TEAM_ROLE_MANAGER
    assert accounts.get_active_membership_for_profile(manager_profile)["role"] == TEAM_ROLE_ADMIN


def test_manager_cannot_leave_or_be_removed_without_transfer(libs):
    _, accounts = libs
    manager_id, manager_profile = accounts.register_user_with_profile("manager@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)
    manager_membership = accounts.get_active_membership_for_profile(manager_profile)

    with pytest.raises(ValueError):
        accounts.leave_team(manager_profile)
    with pytest.raises(ValueError):
        accounts.remove_team_member(int(manager_membership["id"]), removed_by_user_id=manager_id)


def test_member_can_leave_and_be_removed(libs):
    _, accounts = libs
    manager_id, _ = accounts.register_user_with_profile("manager@example.com")
    _, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)
    mid = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager_id)

    accounts.leave_team(member_profile)
    assert accounts.get_active_membership_for_profile(member_profile) is None

    # Rejoin, then have the manager remove them instead.
    mid2 = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid2, accept=True, decided_by_user_id=manager_id)
    accounts.remove_team_member(mid2, removed_by_user_id=manager_id)
    assert accounts.get_active_membership_for_profile(member_profile) is None


# ---------------------------------------------------------- visibility


def test_team_visible_session_seen_by_teammate_not_outsider(libs, session1):
    sessions_lib, accounts = libs
    manager_id, manager_profile = accounts.register_user_with_profile("manager@example.com")
    member_id, member_profile = accounts.register_user_with_profile("member@example.com")
    outsider_id, _ = accounts.register_user_with_profile("outsider@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)
    mid = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager_id)

    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid, manager_profile, uploaded_by_user_id=manager_id)
    accounts.set_session_visibility(sid, VISIBILITY_TEAM)

    visible_to_member = accounts.visible_sessions_for_user(member_id)
    visible_to_outsider = accounts.visible_sessions_for_user(outsider_id)
    assert sid in set(visible_to_member["id"])
    assert sid not in set(visible_to_outsider["id"])

    # 'team' visibility never reaches the public leaderboard/shared browser.
    assert accounts.session_is_publicly_visible(sid) is False
    assert accounts.shareable_reference_sessions().empty
    assert accounts.leaderboard("Test Track").empty


def test_team_visibility_still_requires_a_claimed_owner(libs, session1):
    """Same hard gate as PUBLIC_VISIBILITY_SQL: an unclaimed profile's data
    can never be shared with anyone, team included, no matter what
    visibility an uploader sets or what membership row might exist."""
    sessions_lib, accounts = libs
    uploader_id, _ = accounts.register_user_with_profile("uploader@example.com")
    teammate_id, teammate_profile = accounts.register_user_with_profile("teammate@example.com")
    placeholder, _token = accounts.create_unclaimed_profile("Unclaimed Kid", created_by_user_id=uploader_id)

    team_id = accounts.create_team("Fast Racers", uploader_id)
    mid = accounts.request_to_join_team(team_id, teammate_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=uploader_id)

    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid, placeholder, uploaded_by_user_id=uploader_id)
    accounts.set_session_visibility(sid, VISIBILITY_TEAM)

    assert sid not in set(accounts.visible_sessions_for_user(teammate_id)["id"])


def test_shared_visibility_also_satisfies_team_predicate(libs, session1):
    """A 'shared' session is a superset of 'team' -- a teammate should
    still see it via the team join, same as anyone else would publicly."""
    sessions_lib, accounts = libs
    manager_id, manager_profile = accounts.register_user_with_profile("manager@example.com")
    member_id, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)
    mid = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager_id)

    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid, manager_profile, uploaded_by_user_id=manager_id)
    accounts.set_session_visibility(sid, VISIBILITY_SHARED)

    assert sid in set(accounts.visible_sessions_for_user(member_id)["id"])


def test_private_session_not_visible_to_teammate(libs, session1):
    sessions_lib, accounts = libs
    manager_id, manager_profile = accounts.register_user_with_profile("manager@example.com")
    member_id, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)
    mid = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager_id)

    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid, manager_profile, uploaded_by_user_id=manager_id)
    accounts.set_session_visibility(sid, VISIBILITY_PRIVATE)

    assert sid not in set(accounts.visible_sessions_for_user(member_id)["id"])


def test_set_session_visibility_accepts_team(libs, session1):
    sessions_lib, accounts = libs
    user_id, profile_id = accounts.register_user_with_profile("me@example.com")
    sid = _save(sessions_lib, session1)
    accounts.attribute_session(sid, profile_id, uploaded_by_user_id=user_id)
    accounts.set_session_visibility(sid, VISIBILITY_TEAM)  # should not raise

    with pytest.raises(ValueError):
        accounts.set_session_visibility(sid, "bogus")


# ------------------------------------------------------- team leaderboard


def test_team_leaderboard_and_track_best_times(libs, session1, session2):
    sessions_lib, accounts = libs
    manager_id, manager_profile = accounts.register_user_with_profile("manager@example.com")
    member_id, member_profile = accounts.register_user_with_profile("member@example.com")
    team_id = accounts.create_team("Fast Racers", manager_id)
    mid = accounts.request_to_join_team(team_id, member_profile)
    accounts.resolve_join_request(mid, accept=True, decided_by_user_id=manager_id)

    sid1 = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid1, manager_profile, uploaded_by_user_id=manager_id)
    accounts.set_session_visibility(sid1, VISIBILITY_TEAM)

    sid2 = _save(sessions_lib, session2, track_name="Test Track")
    accounts.attribute_session(sid2, member_profile, uploaded_by_user_id=member_id)
    accounts.set_session_visibility(sid2, VISIBILITY_TEAM)

    assert "Test Track" in accounts.team_leaderboard_tracks()

    board = accounts.team_leaderboard("Test Track")
    assert len(board) == 1  # one team
    assert board.iloc[0]["team_name"] == "Fast Racers"
    assert board.iloc[0]["qualifying_sessions"] == 2

    per_driver = accounts.team_track_best_times(team_id, track_name="Test Track")
    assert set(per_driver["driver_profile_id"]) == {manager_profile, member_profile}
    assert "session_db_id" in per_driver.columns
