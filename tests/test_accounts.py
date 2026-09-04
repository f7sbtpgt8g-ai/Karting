"""Tests for the ownership/attribution/visibility data layer.

The visibility tests carry the most weight here: "an unclaimed driver's data
never becomes public" and "a private session never becomes public" are
security properties, not preferences, so they are asserted from several
angles (row-level check, comparison browser, leaderboard) rather than once.
"""

import os
from datetime import date

import pandas as pd
import pytest

from telemetry.accounts import (
    ATTRIBUTION_CONFIRMED,
    ATTRIBUTION_PENDING,
    ATTRIBUTION_REJECTED,
    CLAIM_CLAIMED,
    CLAIM_INVITED,
    CLAIM_UNCLAIMED,
    CONSENT_GRANTED,
    CONSENT_PENDING,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    AccountLibrary,
    is_minor,
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


# ------------------------------------------------------------------ users


def test_registration_creates_linked_driver_profile(libs):
    _, accounts = libs
    user_id, profile_id = accounts.register_user_with_profile("Ada@Example.com ", display_name="Ada")

    user = accounts.get_user(user_id)
    assert user["email"] == "ada@example.com"  # normalized

    profile = accounts.get_profile(profile_id)
    assert profile["user_id"] == user_id
    assert profile["claim_status"] == CLAIM_CLAIMED
    assert accounts.get_profile_for_user(user_id)["id"] == profile_id


def test_is_minor_boundaries():
    today = date(2026, 9, 4)
    assert is_minor("2011-09-05", today=today) is True  # turns 15 tomorrow
    assert is_minor("2010-09-04", today=today) is False  # exactly 16 today
    assert is_minor("1990-01-01", today=today) is False
    assert is_minor(None, today=today) is False
    assert is_minor("not-a-date", today=today) is False


def test_minor_account_blocked_until_guardian_consent(libs):
    _, accounts = libs
    user_id, _ = accounts.register_user_with_profile(
        "kid@example.com", date_of_birth="2015-01-01", guardian_email="parent@example.com", email_verified=True,
    )
    assert accounts.get_user(user_id)["guardian_consent_status"] == CONSENT_PENDING

    usable, reason = accounts.account_is_usable(user_id)
    assert usable is False
    assert "guardian" in reason.lower()

    accounts.set_guardian_consent(user_id, CONSENT_GRANTED)
    usable, reason = accounts.account_is_usable(user_id)
    assert usable is True and reason is None


def test_unverified_email_blocks_account_use(libs):
    _, accounts = libs
    user_id, _ = accounts.register_user_with_profile("nv@example.com", email_verified=False)
    usable, reason = accounts.account_is_usable(user_id)
    assert usable is False
    assert "verified" in reason.lower()


# -------------------------------------------------------- driver profiles


def test_unclaimed_profile_without_email_gets_no_token(libs):
    _, accounts = libs
    uploader, _ = accounts.register_user_with_profile("up@example.com")
    profile_id, token = accounts.create_unclaimed_profile("Silent Placeholder", created_by_user_id=uploader)

    profile = accounts.get_profile(profile_id)
    assert token is None
    assert profile["claim_status"] == CLAIM_UNCLAIMED
    assert profile["invite_email"] is None
    assert profile["claim_token"] is None


def test_invited_profile_gets_claim_token(libs):
    _, accounts = libs
    uploader, _ = accounts.register_user_with_profile("up@example.com")
    profile_id, token = accounts.create_unclaimed_profile(
        "Invited Driver", created_by_user_id=uploader, invite_email="Invited@Example.com",
    )
    assert token
    profile = accounts.get_profile(profile_id)
    assert profile["claim_status"] == CLAIM_INVITED
    assert profile["invite_email"] == "invited@example.com"
    assert accounts.get_profile_by_claim_token(token)["id"] == profile_id


def test_claiming_by_token_transfers_existing_sessions(libs, session1):
    sessions_lib, accounts = libs
    uploader, _ = accounts.register_user_with_profile("up@example.com")
    profile_id, token = accounts.create_unclaimed_profile(
        "Invited Driver", created_by_user_id=uploader, invite_email="invited@example.com",
    )
    session_db_id = _save(sessions_lib, session1, driver="Invited Driver")
    accounts.attribute_session(session_db_id, profile_id, uploaded_by_user_id=uploader)

    # The claimer registers like anyone else, then links the existing profile.
    claimer = accounts.create_user("invited@example.com", email_verified=True)
    claimed_id = accounts.claim_profile_by_token(token, claimer)

    assert claimed_id == profile_id
    assert accounts.get_profile_for_user(claimer)["id"] == profile_id
    # No data moved -- the session pointed at the profile all along.
    owned = accounts.sessions_for_profile(profile_id)
    assert list(owned["id"]) == [session_db_id]


def test_claim_token_is_single_use(libs):
    _, accounts = libs
    profile_id, token = accounts.create_unclaimed_profile("D", invite_email="d@example.com")
    first = accounts.create_user("d@example.com", email_verified=True)
    accounts.claim_profile_by_token(token, first)

    second = accounts.create_user("someone-else@example.com", email_verified=True)
    assert accounts.get_profile_by_claim_token(token) is None
    with pytest.raises(ValueError):
        accounts.claim_profile_by_token(token, second)


def test_expired_claim_token_is_refused(libs):
    _, accounts = libs
    profile_id, token = accounts.create_unclaimed_profile("D", invite_email="d@example.com")
    with accounts._connect() as conn:
        conn.execute(
            "UPDATE driver_profiles SET claim_token_expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", profile_id),
        )
        conn.commit()
    assert accounts.get_profile_by_claim_token(token) is None


def test_account_cannot_claim_a_second_profile(libs):
    _, accounts = libs
    user_id, _own = accounts.register_user_with_profile("solo@example.com")
    other_id, token = accounts.create_unclaimed_profile("Someone Else", invite_email="se@example.com")

    # Merging two driver identities is refused rather than silently
    # conflating two people's histories.
    with pytest.raises(ValueError, match="already has a driver profile"):
        accounts.claim_profile_by_token(token, user_id)


# ------------------------------------------------------------ attribution


def test_same_user_attribution_is_immediate(libs, session1):
    sessions_lib, accounts = libs
    user_id, profile_id = accounts.register_user_with_profile("me@example.com")
    sid = _save(sessions_lib, session1)

    request_id = accounts.attribute_session(sid, profile_id, uploaded_by_user_id=user_id)
    assert request_id is None
    assert len(accounts.sessions_for_profile(profile_id)) == 1


def test_cross_account_attribution_waits_for_confirmation(libs, session1):
    sessions_lib, accounts = libs
    uploader, _ = accounts.register_user_with_profile("uploader@example.com")
    teammate, teammate_profile = accounts.register_user_with_profile("teammate@example.com")
    sid = _save(sessions_lib, session1)

    request_id = accounts.attribute_session(
        sid, teammate_profile, uploaded_by_user_id=uploader, requires_confirmation=True,
    )
    assert request_id is not None

    # Not in their history until they say so.
    assert accounts.sessions_for_profile(teammate_profile).empty
    assert len(accounts.sessions_for_profile(teammate_profile, include_pending=True)) == 1

    pending = accounts.pending_attribution_requests(teammate_profile)
    assert len(pending) == 1
    assert pending.iloc[0]["requested_by_email"] == "uploader@example.com"

    accounts.resolve_attribution_request(request_id, accept=True)
    assert len(accounts.sessions_for_profile(teammate_profile)) == 1


def test_rejected_attribution_detaches_session_and_leaves_it_with_uploader(libs, session1):
    sessions_lib, accounts = libs
    uploader, _ = accounts.register_user_with_profile("uploader@example.com")
    teammate, teammate_profile = accounts.register_user_with_profile("teammate@example.com")
    sid = _save(sessions_lib, session1)
    request_id = accounts.attribute_session(
        sid, teammate_profile, uploaded_by_user_id=uploader, requires_confirmation=True,
    )

    accounts.resolve_attribution_request(request_id, accept=False)

    assert accounts.sessions_for_profile(teammate_profile, include_pending=True).empty
    row = sessions_lib.list_sessions().set_index("id").loc[sid]
    assert row["attribution_status"] == ATTRIBUTION_REJECTED
    assert pd.isna(row["driver_profile_id"])
    # The upload isn't destroyed -- the uploader still sees it to re-attribute.
    assert sid in set(accounts.visible_sessions_for_user(uploader)["id"])


# ------------------------------------------------------------- visibility


def test_session_defaults_to_private(libs, session1):
    sessions_lib, accounts = libs
    user_id, profile_id = accounts.register_user_with_profile("me@example.com")
    sid = _save(sessions_lib, session1)
    accounts.attribute_session(sid, profile_id, uploaded_by_user_id=user_id)

    assert accounts.session_is_publicly_visible(sid) is False


def test_shared_session_of_claimed_profile_is_public(libs, session1):
    sessions_lib, accounts = libs
    user_id, profile_id = accounts.register_user_with_profile("me@example.com")
    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid, profile_id, uploaded_by_user_id=user_id)
    accounts.set_session_visibility(sid, VISIBILITY_SHARED)

    assert accounts.session_is_publicly_visible(sid) is True


def test_unclaimed_profile_data_is_never_public_even_if_marked_shared(libs, session1):
    """The hard gate: an uploader marking a placeholder's session as shared
    must not make it public, because that driver has never had the chance to
    set their own sharing preference."""
    sessions_lib, accounts = libs
    uploader, _ = accounts.register_user_with_profile("uploader@example.com")
    placeholder, _token = accounts.create_unclaimed_profile("Unclaimed Kid", created_by_user_id=uploader)
    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid, placeholder, uploaded_by_user_id=uploader)
    accounts.set_session_visibility(sid, VISIBILITY_SHARED)  # uploader tries to share it

    assert accounts.session_is_publicly_visible(sid) is False
    assert accounts.shareable_reference_sessions().empty
    assert accounts.leaderboard("Test Track").empty
    assert accounts.leaderboard_tracks() == []


def test_data_becomes_shareable_only_after_claim_and_opt_in(libs, session1):
    sessions_lib, accounts = libs
    uploader, _ = accounts.register_user_with_profile("uploader@example.com")
    placeholder, token = accounts.create_unclaimed_profile(
        "Invited Driver", created_by_user_id=uploader, invite_email="invited@example.com",
    )
    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(sid, placeholder, uploaded_by_user_id=uploader)
    accounts.set_session_visibility(sid, VISIBILITY_SHARED)
    assert accounts.session_is_publicly_visible(sid) is False

    claimer = accounts.create_user("invited@example.com", email_verified=True)
    accounts.claim_profile_by_token(token, claimer)

    # Claiming alone is enough here only because the uploader had already
    # flagged it shared; the driver can immediately set it back.
    assert accounts.session_is_publicly_visible(sid) is True
    accounts.set_session_visibility(sid, VISIBILITY_PRIVATE)
    assert accounts.session_is_publicly_visible(sid) is False


def test_pending_attribution_is_never_public(libs, session1):
    sessions_lib, accounts = libs
    uploader, _ = accounts.register_user_with_profile("uploader@example.com")
    teammate, teammate_profile = accounts.register_user_with_profile("teammate@example.com")
    sid = _save(sessions_lib, session1, track_name="Test Track")
    accounts.attribute_session(
        sid, teammate_profile, uploaded_by_user_id=uploader, requires_confirmation=True,
    )
    accounts.set_session_visibility(sid, VISIBILITY_SHARED)

    assert accounts.session_is_publicly_visible(sid) is False
    assert accounts.leaderboard("Test Track").empty


def test_uploader_keeps_access_to_what_they_uploaded_for_someone_else(libs, session1):
    """A team manager who uploads on a driver's behalf still needs to see
    (and be able to delete) what they uploaded -- otherwise a mistaken
    upload becomes unfixable for the person who made it."""
    sessions_lib, accounts = libs
    uploader, _ = accounts.register_user_with_profile("manager@example.com")
    placeholder, _ = accounts.create_unclaimed_profile("Junior", created_by_user_id=uploader)
    sid = _save(sessions_lib, session1)
    accounts.attribute_session(sid, placeholder, uploaded_by_user_id=uploader)

    assert sid in set(accounts.visible_sessions_for_user(uploader)["id"])
    # ...but a bystander sees nothing.
    bystander, _ = accounts.register_user_with_profile("bystander@example.com")
    assert accounts.visible_sessions_for_user(bystander).empty


def test_visible_sessions_for_user_scopes_correctly(libs, session1, session2):
    sessions_lib, accounts = libs
    alice, alice_profile = accounts.register_user_with_profile("alice@example.com")
    bob, bob_profile = accounts.register_user_with_profile("bob@example.com")

    a_sid = _save(sessions_lib, session1)
    accounts.attribute_session(a_sid, alice_profile, uploaded_by_user_id=alice)

    b_sid = _save(sessions_lib, session2)
    accounts.attribute_session(b_sid, bob_profile, uploaded_by_user_id=bob)

    # Bob's stays private -> Alice can't see it.
    assert set(accounts.visible_sessions_for_user(alice)["id"]) == {a_sid}

    accounts.set_session_visibility(b_sid, VISIBILITY_SHARED)
    assert set(accounts.visible_sessions_for_user(alice)["id"]) == {a_sid, b_sid}


# ----------------------------------------------------------- leaderboards


def _shared_session(sessions_lib, accounts, session, email, name, *, best_lap=None, **save_kwargs):
    user_id, profile_id = accounts.register_user_with_profile(email, display_name=name)
    sid = sessions_lib.save_session(session, **save_kwargs)
    accounts.attribute_session(sid, profile_id, uploaded_by_user_id=user_id)
    accounts.set_session_visibility(sid, VISIBILITY_SHARED)
    if best_lap is not None:
        with accounts._connect() as conn:
            conn.execute("UPDATE sessions SET best_lap_s = ? WHERE id = ?", (best_lap, sid))
            conn.commit()
    return user_id, profile_id, sid


def test_leaderboard_ranks_by_best_lap_and_filters_by_conditions(libs, session1, session2):
    sessions_lib, accounts = libs
    _shared_session(
        sessions_lib, accounts, session1, "fast@example.com", "Fast Driver",
        best_lap=29.5, track_name="Ring", track_condition="Dry", kart_class="Rotax Senior EVO",
    )
    _shared_session(
        sessions_lib, accounts, session2, "slow@example.com", "Slow Driver",
        best_lap=31.2, track_name="Ring", track_condition="Wet", kart_class="Rotax Senior EVO",
    )

    overall = accounts.leaderboard("Ring")
    assert list(overall["driver_display_name"]) == ["Fast Driver", "Slow Driver"]
    assert list(overall["rank"]) == [1, 2]

    dry = accounts.leaderboard("Ring", track_condition="Dry")
    assert list(dry["driver_display_name"]) == ["Fast Driver"]

    wet = accounts.leaderboard("Ring", track_condition="Wet")
    assert list(wet["driver_display_name"]) == ["Slow Driver"]

    assert accounts.leaderboard("Ring", kart_class="Some Other Class").empty


def test_leaderboard_excludes_private_sessions(libs, session1, session2):
    sessions_lib, accounts = libs
    _shared_session(
        sessions_lib, accounts, session1, "shared@example.com", "Shares", best_lap=30.0, track_name="Ring",
    )
    private_user, private_profile = accounts.register_user_with_profile("private@example.com", display_name="Private")
    p_sid = sessions_lib.save_session(session2, track_name="Ring")
    accounts.attribute_session(p_sid, private_profile, uploaded_by_user_id=private_user)
    with accounts._connect() as conn:
        conn.execute("UPDATE sessions SET best_lap_s = ? WHERE id = ?", (25.0, p_sid))  # would be P1
        conn.commit()

    board = accounts.leaderboard("Ring")
    assert list(board["driver_display_name"]) == ["Shares"]


def test_shareable_reference_sessions_excludes_own_and_filters(libs, session1, session2):
    sessions_lib, accounts = libs
    me, my_profile = accounts.register_user_with_profile("me@example.com", display_name="Me")
    my_sid = sessions_lib.save_session(session1, track_name="Ring")
    accounts.attribute_session(my_sid, my_profile, uploaded_by_user_id=me)
    accounts.set_session_visibility(my_sid, VISIBILITY_SHARED)

    _, _, their_sid = _shared_session(
        sessions_lib, accounts, session2, "them@example.com", "Them",
        track_name="Ring", track_condition="Dry",
    )

    browsable = accounts.shareable_reference_sessions(exclude_user_id=me)
    assert set(browsable["id"]) == {their_sid}
    assert accounts.shareable_reference_sessions(exclude_user_id=me, track_name="Nowhere").empty
    assert set(
        accounts.shareable_reference_sessions(exclude_user_id=me, driver_query="the")["id"]
    ) == {their_sid}
