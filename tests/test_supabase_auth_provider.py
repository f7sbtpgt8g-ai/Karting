"""Tests for SupabaseAuthProvider, which until now was documented in-code as
"never run against a live project."

That matters more than it sounds: this provider is what populates
`users.external_auth_id`, and that column is what every RLS policy resolves
the caller through (`current_app_user_id()`). A provider that authenticates
someone successfully but leaves that column NULL produces a browser client
that is definitely signed in and definitely sees no data, with no error
raised anywhere -- so the linking behaviour is tested here explicitly rather
than assumed.

GoTrue itself is stubbed at the HTTP boundary (`_post`), so these run with no
network and no Supabase project. `scripts/verify_supabase_auth.py` covers the
real thing.
"""

from __future__ import annotations

import os

import pytest

from telemetry.accounts import AccountLibrary
from telemetry.auth import AuthMirrorConflict, AuthStore, SupabaseAuthProvider

SUPA_UID = "11111111-1111-1111-1111-111111111111"
OTHER_UID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def accounts(tmp_path):
    return AccountLibrary(os.path.join(tmp_path, "accounts.db"))


@pytest.fixture
def store(tmp_path):
    return AuthStore(os.path.join(tmp_path, "accounts.db"))


class StubGoTrue(SupabaseAuthProvider):
    """SupabaseAuthProvider with the HTTP call replaced by canned responses,
    recording what it was asked for so the request shape can be asserted."""

    def __init__(self, accounts, store, responses):
        super().__init__(accounts, store, "https://example.supabase.co", "anon-key")
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _post(self, path, payload):
        self.calls.append((path, payload))
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response
        return (404, {"msg": "no stub for " + path})


def _signup_ok(uid=SUPA_UID):
    return (200, {"user": {"id": uid, "email": "driver@example.com"}})


def _login_ok(uid=SUPA_UID, confirmed=True):
    user = {"id": uid, "email": "driver@example.com"}
    if confirmed:
        user["email_confirmed_at"] = "2026-01-01T00:00:00Z"
    return (200, {"user": user, "access_token": "jwt"})


def test_registration_links_the_supabase_identity(accounts, store):
    provider = StubGoTrue(accounts, store, {"/signup": _signup_ok()})
    result = provider.register("Driver@Example.com", "correct horse battery", display_name="Driver")

    assert result.ok, result.error
    user = accounts.get_user(result.user_id)
    assert user["external_auth_id"] == SUPA_UID, "RLS cannot resolve a user without this"
    assert user["email"] == "driver@example.com"
    # Same invisible-plumbing behaviour as the local provider.
    assert accounts.get_profile_for_user(result.user_id) is not None


def test_login_links_the_supabase_identity(accounts, store):
    provider = StubGoTrue(accounts, store, {"/token": _login_ok()})
    result = provider.login("driver@example.com", "correct horse battery")

    assert result.ok, result.error
    assert accounts.get_user(result.user_id)["external_auth_id"] == SUPA_UID


def test_existing_local_account_is_backfilled_on_first_supabase_login(accounts, store):
    """The migration path that actually matters: an account created under
    the old local PBKDF2 provider has external_auth_id NULL. Signing in
    through Supabase for the first time must link it, or that user stays
    invisible to every RLS policy forever."""
    legacy_id, _ = accounts.register_user_with_profile(
        "driver@example.com", password_hash="pbkdf2-hash", display_name="Driver"
    )
    assert accounts.get_user(legacy_id)["external_auth_id"] is None

    provider = StubGoTrue(accounts, store, {"/token": _login_ok()})
    result = provider.login("driver@example.com", "correct horse battery")

    assert result.ok, result.error
    assert result.user_id == legacy_id, "should adopt the existing account, not create a second one"
    assert accounts.get_user(legacy_id)["external_auth_id"] == SUPA_UID


def test_a_different_supabase_identity_cannot_adopt_a_linked_account(accounts, store):
    """Two Supabase users must never collapse onto one local account -- that
    would hand one driver's telemetry to another."""
    first = StubGoTrue(accounts, store, {"/token": _login_ok(SUPA_UID)})
    first_result = first.login("driver@example.com", "pw")
    assert first_result.ok

    second = StubGoTrue(accounts, store, {"/token": _login_ok(OTHER_UID)})
    second_result = second.login("driver@example.com", "pw")

    assert not second_result.ok
    assert "already linked" in (second_result.error or "")
    # The original link is untouched.
    assert accounts.get_user(first_result.user_id)["external_auth_id"] == SUPA_UID


def test_mirror_conflict_is_raised_not_swallowed(accounts, store):
    provider = StubGoTrue(accounts, store, {})
    accounts.register_user_with_profile("driver@example.com", external_auth_id=SUPA_UID)
    with pytest.raises(AuthMirrorConflict):
        provider._mirror_user(OTHER_UID, "driver@example.com")


def test_repeat_login_is_idempotent(accounts, store):
    provider = StubGoTrue(accounts, store, {"/token": _login_ok()})
    first = provider.login("driver@example.com", "pw")
    second = provider.login("driver@example.com", "pw")

    assert first.user_id == second.user_id
    assert len(accounts.list_profiles()) == 1, "repeated login should not fork a second profile"


def test_login_marks_email_verified_from_supabase_state(accounts, store):
    """Supabase owns verification state; the local mirror follows it."""
    provider = StubGoTrue(accounts, store, {"/token": _login_ok(confirmed=True)})
    result = provider.login("driver@example.com", "pw")
    assert accounts.get_user(result.user_id)["email_verified"]


def test_failed_login_creates_no_local_account(accounts, store):
    provider = StubGoTrue(accounts, store, {"/token": (400, {"msg": "Invalid login credentials"})})
    result = provider.login("driver@example.com", "wrong")

    assert not result.ok
    assert accounts.get_user_by_email("driver@example.com") is None


def test_registration_rejects_a_weak_password_before_calling_supabase(accounts, store):
    provider = StubGoTrue(accounts, store, {"/signup": _signup_ok()})
    result = provider.register("driver@example.com", "short")

    assert not result.ok
    assert provider.calls == [], "should not have hit the auth service at all"


def test_under_16_still_requires_a_guardian(accounts, store):
    """The consent gate is policy, not a local-provider implementation
    detail -- it must hold whichever backend is active."""
    provider = StubGoTrue(accounts, store, {"/signup": _signup_ok()})
    result = provider.register(
        "kid@example.com", "correct horse battery", date_of_birth="2015-01-01"
    )

    assert not result.ok
    assert "guardian" in (result.error or "").lower()
    assert provider.calls == []
