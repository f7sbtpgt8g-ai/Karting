"""Tests for the local auth provider, token lifecycle, session management,
and the email outbox (including the invite-send gate).

The Supabase provider is not exercised here -- it talks to an external
service and there is no live project to point at; its request shapes are
documented in `auth.py` and the first real deployment is its integration
test.
"""

import os

import pytest

from telemetry.accounts import AccountLibrary, CONSENT_PENDING
from telemetry.auth import (
    TOKEN_PASSWORD_RESET,
    TOKEN_VERIFY_EMAIL,
    AuthStore,
    LocalAuthProvider,
    hash_password,
    provider_from_env,
    validate_password,
    verify_password,
)
from telemetry.mailer import (
    OutboxEmailSender,
    SmtpEmailSender,
    claim_invite_email,
    invite_emails_enabled,
    password_reset_email,
)
from telemetry.storage import SessionLibrary


@pytest.fixture
def auth(tmp_path):
    db_path = os.path.join(tmp_path, "sessions.db")
    SessionLibrary(db_path)  # creates the sessions table the FKs reference
    accounts = AccountLibrary(db_path)
    store = AuthStore(db_path)
    return accounts, store, LocalAuthProvider(accounts, store), db_path


# ------------------------------------------------------------- passwords


def test_password_hash_roundtrip_and_uniqueness():
    encoded = hash_password("correct horse battery")
    assert verify_password("correct horse battery", encoded) is True
    assert verify_password("wrong", encoded) is False
    # Distinct salts -> the same password never produces the same hash.
    assert hash_password("correct horse battery") != encoded


def test_verify_password_handles_missing_or_malformed_hash():
    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False
    assert verify_password("anything", "not-a-real-hash") is False
    assert verify_password("anything", "md5$1$aa$bb") is False


def test_validate_password_length():
    assert validate_password("short") is not None
    assert validate_password("longenough123") is None


# ----------------------------------------------------------- registration


def test_register_creates_verified_path_and_profile(auth):
    accounts, store, provider, _ = auth
    result = provider.register("New@Example.com", "longenough123", display_name="New Driver")

    assert result.ok
    assert result.token  # verification token to email
    user = accounts.get_user(result.user_id)
    assert user["email"] == "new@example.com"
    assert user["email_verified"] == 0
    assert accounts.get_profile_for_user(result.user_id) is not None

    verified = provider.verify_email(result.token)
    assert verified.ok
    assert accounts.get_user(result.user_id)["email_verified"] == 1


def test_register_rejects_duplicate_email(auth):
    _, _, provider, _ = auth
    provider.register("dupe@example.com", "longenough123")
    second = provider.register("DUPE@example.com", "longenough123")
    assert second.ok is False
    assert "already exists" in second.error


def test_register_rejects_weak_password(auth):
    _, _, provider, _ = auth
    result = provider.register("weak@example.com", "short")
    assert result.ok is False
    assert "at least" in result.error


def test_minor_registration_requires_guardian_email(auth):
    accounts, _, provider, _ = auth
    refused = provider.register("kid@example.com", "longenough123", date_of_birth="2015-05-05")
    assert refused.ok is False
    assert "guardian" in refused.error.lower()

    allowed = provider.register(
        "kid@example.com", "longenough123", date_of_birth="2015-05-05", guardian_email="parent@example.com",
    )
    assert allowed.ok
    assert accounts.get_user(allowed.user_id)["guardian_consent_status"] == CONSENT_PENDING


# ------------------------------------------------------------------ login


def test_login_success_and_failure(auth):
    accounts, _, provider, _ = auth
    registered = provider.register("login@example.com", "longenough123")

    good = provider.login("login@example.com", "longenough123")
    assert good.ok and good.user_id == registered.user_id
    assert accounts.get_user(registered.user_id)["last_login_at"] is not None

    bad = provider.login("login@example.com", "wrongpassword")
    assert bad.ok is False


def test_login_does_not_leak_whether_an_account_exists(auth):
    _, _, provider, _ = auth
    provider.register("real@example.com", "longenough123")
    wrong_password = provider.login("real@example.com", "nottherightone")
    no_such_account = provider.login("ghost@example.com", "nottherightone")
    assert wrong_password.error == no_such_account.error


# ------------------------------------------------------------ reset flow


def test_password_reset_flow_revokes_existing_sessions(auth):
    accounts, store, provider, _ = auth
    registered = provider.register("reset@example.com", "longenough123")
    live_session = store.start_session(registered.user_id)
    assert store.user_for_session(live_session) == registered.user_id

    requested = provider.request_password_reset("reset@example.com")
    assert requested.ok and requested.token

    done = provider.reset_password(requested.token, "brandnewpassword")
    assert done.ok
    assert provider.login("reset@example.com", "brandnewpassword").ok
    assert provider.login("reset@example.com", "longenough123").ok is False
    # An attacker's pre-existing session must not survive the reset.
    assert store.user_for_session(live_session) is None


def test_password_reset_for_unknown_email_reports_success_without_token(auth):
    _, _, provider, _ = auth
    result = provider.request_password_reset("nobody@example.com")
    assert result.ok is True
    assert result.token is None


def test_reset_token_is_single_use(auth):
    _, _, provider, _ = auth
    provider.register("once@example.com", "longenough123")
    token = provider.request_password_reset("once@example.com").token

    assert provider.reset_password(token, "firstnewpassword").ok
    replayed = provider.reset_password(token, "secondnewpassword")
    assert replayed.ok is False


def test_expired_token_is_refused(auth):
    accounts, store, provider, _ = auth
    registered = provider.register("exp@example.com", "longenough123")
    token = store.issue_token(registered.user_id, TOKEN_VERIFY_EMAIL, ttl_hours=-1)
    assert store.consume_token(token, TOKEN_VERIFY_EMAIL) is None


def test_token_kind_is_enforced(auth):
    _, store, provider, _ = auth
    registered = provider.register("kind@example.com", "longenough123")
    token = store.issue_token(registered.user_id, TOKEN_VERIFY_EMAIL, ttl_hours=1)
    assert store.consume_token(token, TOKEN_PASSWORD_RESET) is None
    assert store.consume_token(token, TOKEN_VERIFY_EMAIL) == registered.user_id


# --------------------------------------------------------------- sessions


def test_session_lifecycle(auth):
    _, store, provider, _ = auth
    registered = provider.register("sess@example.com", "longenough123")
    token = store.start_session(registered.user_id)

    assert store.user_for_session(token) == registered.user_id
    store.revoke_session(token)
    assert store.user_for_session(token) is None
    assert store.user_for_session(None) is None
    assert store.user_for_session("made-up-token") is None


# ----------------------------------------------------------------- mailer


def test_outbox_records_without_sending(auth):
    _, _, _, db_path = auth
    sender = OutboxEmailSender(db_path)
    sent = sender.send(password_reset_email("someone@example.com", "https://example.com/reset?t=abc"))

    assert sent is False  # nothing actually delivered
    recorded = sender.outbox(kind="password_reset")
    assert len(recorded) == 1
    assert "https://example.com/reset?t=abc" in recorded[0]["body"]
    assert recorded[0]["suppressed_reason"] is None


def test_invite_email_is_suppressed_unless_explicitly_enabled(auth, monkeypatch):
    _, _, _, db_path = auth
    monkeypatch.delenv("KARTING_ENABLE_INVITE_EMAILS", raising=False)
    assert invite_emails_enabled() is False

    sender = OutboxEmailSender(db_path)
    sender.send(claim_invite_email("kid@example.com", "Sam", "Team Manager", "Ring, 2026-08-15", "https://x/claim"))

    recorded = sender.outbox(kind="claim_invite")
    assert len(recorded) == 1
    assert recorded[0]["sent"] == 0
    assert "disabled" in recorded[0]["suppressed_reason"]


def test_invite_gate_reads_env(monkeypatch):
    monkeypatch.setenv("KARTING_ENABLE_INVITE_EMAILS", "1")
    assert invite_emails_enabled() is True
    monkeypatch.setenv("KARTING_ENABLE_INVITE_EMAILS", "no")
    assert invite_emails_enabled() is False


def test_smtp_sender_does_not_send_suppressed_invites(auth, monkeypatch, tmp_path):
    """The gate has to hold on the real sender too, not just the outbox
    one -- otherwise configuring SMTP would silently start sending
    unsolicited invites."""
    _, _, _, db_path = auth
    monkeypatch.delenv("KARTING_ENABLE_INVITE_EMAILS", raising=False)
    sender = SmtpEmailSender(db_path, host="localhost", port=1)  # would fail if it tried to connect

    delivered = sender.send(
        claim_invite_email("kid@example.com", "Sam", "Team Manager", "Ring, 2026-08-15", "https://x/claim")
    )
    assert delivered is False
    recorded = sender.outbox_sender.outbox(kind="claim_invite")
    assert recorded[0]["sent"] == 0


def test_guardian_consent_copy_describes_the_sharing_default_accurately():
    """Sessions default to shared, so the consent a parent gives has to say
    so -- this copy previously promised the opposite and would have made
    their approval uninformed."""
    from telemetry.mailer import guardian_consent_email

    body = guardian_consent_email("parent@example.com", "Sam", "https://x/consent").body.lower()
    assert "shared by default" in body
    assert "leaderboard" in body
    assert "private" in body  # the opt-out is stated too
    assert "private by default" not in body  # the old, now-false claim


def test_claim_invite_copy_warns_that_claiming_shares_by_default():
    email = claim_invite_email("kid@example.com", "Sam", "Team Manager", "Ring, 2026-08-15", "https://x/claim")
    body = email.body.lower()
    assert "shared by default" in body
    assert "leaderboard" in body


def test_claim_invite_copy_offers_deletion_as_prominently_as_signup():
    """The invite goes to someone who never consented to be contacted, so
    declining has to be a real, stated option -- not buried."""
    email = claim_invite_email("kid@example.com", "Sam", "Team Manager", "Ring, 2026-08-15", "https://x/claim")
    body = email.body.lower()
    assert "delete" in body
    assert "don't need to create an account" in body
    assert "private" in body
    assert "parent or guardian" in body


# --------------------------------------------------------------- provider


def test_provider_from_env_defaults_to_local(auth, monkeypatch):
    accounts, store, _, _ = auth
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    assert provider_from_env(accounts, store).name == "local"


def test_provider_from_env_selects_supabase_when_configured(auth, monkeypatch):
    accounts, store, _, _ = auth
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    assert provider_from_env(accounts, store).name == "supabase"
