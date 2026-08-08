"""The login, and the things it must refuse.

These lean on the failure modes rather than the happy path, because the happy
path is the one anybody would notice was broken. A guard that lets the wrong
person in looks exactly like a guard that works.

Everything runs inside the ordinary rolled-back fixture: the guard hangs off
the /api router as a dependency, so it resolves the same injected session the
endpoints do and a test never has to commit a real user to exercise it.
"""


import pyotp
import pytest
from sqlalchemy import delete, select

from backend.auth import hash_password, verify_password
from backend.models import AppUser, RecoveryCode
from backend.plaid_client import decrypt_token

PASSWORD = "a-long-enough-password"


@pytest.fixture
def wipe_users(session):
    """Start from no user. The fixture's rollback handles the cleanup."""
    session.execute(delete(RecoveryCode))
    session.execute(delete(AppUser))
    session.flush()
    return session


def totp_for(secret_uri: str) -> str:
    return pyotp.TOTP(secret_uri).now()


def setup_user(client, username="ardome") -> dict:
    response = client.post(
        "/api/auth/setup", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


def secret_from(body: dict) -> str:
    return pyotp.parse_uri(body["otpauth_uri"]).secret


# --- Setup ---------------------------------------------------------------


def test_setup_returns_a_scannable_secret_and_recovery_codes(client, wipe_users):
    body = setup_user(client)
    assert body["qr_svg"].lstrip().startswith("<?xml") or "<svg" in body["qr_svg"]
    assert len(body["recovery_codes"]) == 10
    assert len(set(body["recovery_codes"])) == 10, "codes must not repeat"
    assert secret_from(body)


def test_setup_refuses_once_a_user_is_confirmed(client, wipe_users):
    body = setup_user(client)
    client.post(
        "/api/auth/setup/confirm", json={"code": totp_for(secret_from(body))}
    )

    again = client.post(
        "/api/auth/setup", json={"username": "someone-else", "password": PASSWORD}
    )
    assert again.status_code == 409, "the setup window must close permanently"


def test_setup_can_be_restarted_while_unconfirmed(client, wipe_users):
    """A scan that went wrong must not brick the only account there will be."""
    setup_user(client)
    retry = client.post(
        "/api/auth/setup", json={"username": "ardome", "password": PASSWORD}
    )
    assert retry.status_code == 200


def test_confirm_rejects_a_wrong_code(client, wipe_users):
    setup_user(client)
    assert client.post("/api/auth/setup/confirm", json={"code": "000000"}).status_code == 401


def test_a_short_password_is_refused(client, wipe_users):
    response = client.post(
        "/api/auth/setup", json={"username": "ardome", "password": "short"}
    )
    assert response.status_code == 422


# --- Login ---------------------------------------------------------------


def test_login_needs_both_factors(client, wipe_users):
    body = setup_user(client)
    secret = secret_from(body)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret)})
    client.post("/api/auth/logout")

    right_password_no_code = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": PASSWORD, "code": "000000"},
    )
    assert right_password_no_code.status_code == 401

    wrong_password_right_code = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": "wrong-password", "code": totp_for(secret)},
    )
    assert wrong_password_right_code.status_code == 401

    both = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": PASSWORD, "code": totp_for(secret)},
    )
    assert both.status_code == 200


def test_the_error_does_not_say_which_factor_was_wrong(client, wipe_users):
    """Telling an attacker the password was right confirms half the credential."""
    body = setup_user(client)
    secret = secret_from(body)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret)})
    client.post("/api/auth/logout")

    bad_password = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": "nope-nope-nope", "code": totp_for(secret)},
    ).json()["detail"]
    bad_code = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": PASSWORD, "code": "000000"},
    ).json()["detail"]

    assert bad_password == bad_code


def test_a_recovery_code_works_once_and_only_once(client, wipe_users):
    body = setup_user(client)
    secret = secret_from(body)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret)})
    client.post("/api/auth/logout")
    code = body["recovery_codes"][0]

    first = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": PASSWORD, "code": code},
    )
    assert first.status_code == 200
    client.post("/api/auth/logout")

    second = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": PASSWORD, "code": code},
    )
    assert second.status_code == 401, "a spent recovery code must not work again"


def test_too_many_failures_locks_the_account(client, wipe_users, session):
    body = setup_user(client)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret_from(body))})
    client.post("/api/auth/logout")

    for _ in range(8):
        client.post(
            "/api/auth/login",
            json={"username": "ardome", "password": "wrong-password", "code": "000000"},
        )

    locked = client.post(
        "/api/auth/login",
        json={
            "username": "ardome",
            "password": PASSWORD,
            "code": totp_for(secret_from(body)),
        },
    )
    assert locked.status_code == 429, "correct credentials must still be refused"


# --- Storage -------------------------------------------------------------


def test_the_password_is_not_recoverable_from_the_database(client, session, wipe_users):
    setup_user(client)
    user = session.scalars(select(AppUser)).first()
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2")
    assert verify_password(user.password_hash, PASSWORD)


def test_the_totp_secret_is_encrypted_at_rest(client, session, wipe_users):
    """An attacker with the database must not be able to mint valid codes."""
    body = setup_user(client)
    plain = secret_from(body)
    user = session.scalars(select(AppUser)).first()
    assert plain not in user.totp_secret
    assert decrypt_token(user.totp_secret) == plain


def test_recovery_codes_are_hashed(client, session, wipe_users):
    body = setup_user(client)
    first = body["recovery_codes"][0]
    stored = [c.code_hash for c in session.scalars(select(RecoveryCode)).all()]
    assert first not in stored
    assert any(verify_password(h, first) for h in stored)


# --- The guard -----------------------------------------------------------


def test_the_api_is_open_before_setup(client, wipe_users):
    """First launch cannot require a login that does not exist yet."""
    assert client.get("/api/categories").status_code == 200


def test_the_api_is_closed_once_a_user_exists(client, wipe_users):
    body = setup_user(client)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret_from(body))})
    client.post("/api/auth/logout")

    assert client.get("/api/categories").status_code == 401
    assert client.get("/api/overview").status_code == 401
    assert client.post("/api/plaid/sync").status_code == 401


def test_signing_in_reopens_the_api(client, wipe_users):
    body = setup_user(client)
    secret = secret_from(body)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret)})
    client.post("/api/auth/logout")
    assert client.get("/api/categories").status_code == 401

    client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": PASSWORD, "code": totp_for(secret)},
    )
    assert client.get("/api/categories").status_code == 200


def test_health_stays_public(client, wipe_users):
    body = setup_user(client)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret_from(body))})
    client.post("/api/auth/logout")
    assert client.get("/api/health").status_code == 200


def test_an_expired_session_is_refused(client, wipe_users, monkeypatch):
    """Expiry is enforced server-side; a cookie's own max-age is only a hint,
    and an attacker replaying a stolen cookie is free to ignore it."""
    from backend import auth as auth_module

    body = setup_user(client)
    client.post("/api/auth/setup/confirm", json={"code": totp_for(secret_from(body))})
    assert client.get("/api/categories").status_code == 200

    # The cookie is untouched and still validly signed; only the issue time
    # recorded inside it is now outside the window.
    monkeypatch.setattr(auth_module.settings, "session_days", 0)
    assert client.get("/api/categories").status_code == 401


def test_the_session_cookie_is_http_only(client, wipe_users):
    body = setup_user(client)
    response = client.post(
        "/api/auth/setup/confirm", json={"code": totp_for(secret_from(body))}
    )
    cookie = response.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower(), "script on the page must not read it"
    assert "samesite=lax" in cookie.lower().replace(" ", "")


# --- Hashing -------------------------------------------------------------


def test_the_same_password_hashes_differently_each_time(client):
    """Per-hash salt: two identical passwords must not share a hash."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)
