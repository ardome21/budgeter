"""Passkeys: what the endpoints refuse.

A real ceremony needs an authenticator holding a private key, which no test
has. What *is* testable without one is every decision made around the
signature — who may register, what happens with no challenge in flight, that an
unknown credential is refused, and that a stalled counter is treated as a clone.

Those are the parts that would be wrong in a way nobody notices. A broken
signature check fails loudly the first time anyone tries to sign in.
"""

from datetime import UTC, datetime

import pyotp
import pytest
from sqlalchemy import select

from backend.models import AppUser, Passkey

PASSWORD = "a-long-enough-password"


@pytest.fixture
def signed_in(client, session):
    """A confirmed user with a live session, and their TOTP secret."""
    # No clearing here: the `client` fixture already deletes any user through
    # the ORM, cascades included. Repeating it as bulk SQL is what broke once,
    # by deleting a user whose passkeys still referenced it.
    body = client.post(
        "/api/auth/setup", json={"username": "ardome", "password": PASSWORD}
    ).json()
    secret = pyotp.parse_uri(body["otpauth_uri"]).secret
    client.post("/api/auth/setup/confirm", json={"code": pyotp.TOTP(secret).now()})
    return secret


def add_passkey(session, *, credential_id="cred-1", sign_count=5) -> Passkey:
    user = session.scalars(select(AppUser)).first()
    passkey = Passkey(
        user_id=user.id,
        credential_id=credential_id,
        public_key="not-a-real-key",
        sign_count=sign_count,
        label="MacBook Touch ID",
        created_at=datetime.now(UTC),
    )
    session.add(passkey)
    session.flush()
    return passkey


# --- Who may register ----------------------------------------------------


def test_registering_requires_a_session(client, session, signed_in):
    """Adding a passkey sets a credential, so it sits behind the login."""
    client.post("/api/auth/logout")
    assert client.post("/api/auth/passkeys/register/begin").status_code == 401


def test_register_begin_returns_options_when_signed_in(client, signed_in):
    body = client.post("/api/auth/passkeys/register/begin").json()

    assert body["rp"]["id"] == "localhost"
    assert body["challenge"]
    assert body["authenticatorSelection"]["userVerification"] == "required"
    assert body["authenticatorSelection"]["residentKey"] == "required"


def test_registering_excludes_passkeys_already_held(client, session, signed_in):
    """Otherwise one authenticator registers twice and looks like two devices."""
    add_passkey(session, credential_id="Y3JlZC0x")
    body = client.post("/api/auth/passkeys/register/begin").json()
    assert len(body["excludeCredentials"]) == 1


def test_register_finish_without_a_challenge_is_refused(client, signed_in):
    response = client.post(
        "/api/auth/passkeys/register/finish",
        json={"label": "Mac", "credential": {"id": "whatever"}},
    )
    assert response.status_code == 400
    assert "start again" in response.json()["detail"]


# --- Signing in ----------------------------------------------------------


def test_login_begin_needs_a_registered_passkey(client, session, signed_in):
    client.post("/api/auth/logout")
    assert client.post("/api/auth/passkeys/login/begin").status_code == 404

    add_passkey(session)
    body = client.post("/api/auth/passkeys/login/begin").json()
    assert body["challenge"]
    assert body["userVerification"] == "required"


def test_login_is_reachable_while_signed_out(client, session, signed_in):
    """The whole point: it is how you get back in."""
    add_passkey(session)
    client.post("/api/auth/logout")
    assert client.post("/api/auth/passkeys/login/begin").status_code == 200


def test_login_finish_without_a_challenge_is_refused(client, session, signed_in):
    add_passkey(session)
    client.post("/api/auth/logout")
    response = client.post(
        "/api/auth/passkeys/login/finish", json={"credential": {"id": "cred-1"}}
    )
    assert response.status_code == 400


def test_an_unknown_credential_is_refused(client, session, signed_in):
    add_passkey(session, credential_id="known")
    client.post("/api/auth/logout")
    client.post("/api/auth/passkeys/login/begin")

    response = client.post(
        "/api/auth/passkeys/login/finish", json={"credential": {"id": "not-known"}}
    )
    assert response.status_code == 401
    assert "not registered" in response.json()["detail"]


# --- Managing them -------------------------------------------------------


def test_listing_requires_a_session(client, session, signed_in):
    add_passkey(session)
    assert client.get("/api/auth/passkeys").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/passkeys").status_code == 401


def test_a_passkey_can_be_revoked(client, session, signed_in):
    passkey = add_passkey(session)
    assert client.delete(f"/api/auth/passkeys/{passkey.id}").status_code == 200
    assert session.get(Passkey, passkey.id) is None


def test_revoking_leaves_the_password_login_working(client, session, signed_in):
    """Removing every passkey must not lock anyone out."""
    passkey = add_passkey(session)
    client.delete(f"/api/auth/passkeys/{passkey.id}")
    client.post("/api/auth/logout")

    response = client.post(
        "/api/auth/login",
        json={
            "username": "ardome",
            "password": PASSWORD,
            "code": pyotp.TOTP(signed_in).now(),
        },
    )
    assert response.status_code == 200


def test_revoking_something_that_is_not_yours_is_a_404(client, session, signed_in):
    other = AppUser(
        username="someone-else",
        password_hash="x",
        totp_secret="x",
        totp_confirmed=True,
        failed_logins=0,
        created_at=datetime.now(UTC),
    )
    session.add(other)
    session.flush()
    theirs = Passkey(
        user_id=other.id,
        credential_id="theirs",
        public_key="k",
        sign_count=0,
        label="Not yours",
        created_at=datetime.now(UTC),
    )
    session.add(theirs)
    session.flush()

    assert client.delete(f"/api/auth/passkeys/{theirs.id}").status_code == 404
    assert session.get(Passkey, theirs.id) is not None
