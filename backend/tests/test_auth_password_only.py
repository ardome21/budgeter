"""Signing in when no second factor is in force.

This is the default. `require_totp` is off because of what this deployment
actually is: one user, bound to loopback, where an attacker who could supply a
password but not a code would have to already be typing on the machine that
holds the database — and could read it directly. What the code reliably did
instead was stand between the owner and their own budget, twice, and the
endpoint of that is a login that gets switched off entirely.

test_auth.py covers the same endpoints with it switched back on.
"""

import pytest
from sqlalchemy import select

from backend.models import AppUser, RecoveryCode

PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def _mfa_off(monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "require_totp", False)


def setup_user(client, username="ardome") -> dict:
    response = client.post(
        "/api/auth/setup", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_setup_is_one_step(client, session):
    """No QR, no codes to copy down, and the account is usable immediately.

    The two-step enrolment exists to prove an authenticator was really scanned.
    With no authenticator in the picture it only leaves the account in a state
    that cannot log in — which is exactly how this app locked its owner out.
    """
    body = setup_user(client)

    assert body["qr_svg"] == ""
    assert body["otpauth_uri"] == ""
    assert body["recovery_codes"] == []
    user = session.scalar(select(AppUser))
    assert user.totp_confirmed is True


def test_setup_signs_you_straight_in(client):
    """There is nothing left to prove, so there is nothing left to click."""
    setup_user(client)

    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_no_recovery_codes_are_minted(client, session):
    """A recovery code is a way *past* a second factor. With none in force they
    are ten more secrets to store for nothing."""
    setup_user(client)

    assert session.scalars(select(RecoveryCode)).all() == []


def test_the_password_alone_signs_you_in(client):
    setup_user(client)
    client.post("/api/auth/logout")

    response = client.post(
        "/api/auth/login", json={"username": "ardome", "password": PASSWORD}
    )

    assert response.status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_a_wrong_password_is_still_refused(client):
    """Turning off the second factor does not turn off the first one."""
    setup_user(client)
    client.post("/api/auth/logout")

    response = client.post(
        "/api/auth/login", json={"username": "ardome", "password": "not-it"}
    )

    assert response.status_code == 401
    assert client.get("/api/categories").status_code == 401


def test_the_api_is_still_closed_without_a_session(client):
    """The guard is unchanged. One factor is not no factor."""
    setup_user(client)
    client.post("/api/auth/logout")

    assert client.get("/api/categories").status_code == 401
    assert client.get("/api/net-worth").status_code == 401


def test_a_secret_is_still_stored_so_it_can_be_turned_back_on(client, session):
    """Switching `require_totp` on later must be a config change, not a
    re-enrolment — `scripts/show_code.py --setup-key` prints this when needed."""
    setup_user(client)

    user = session.scalar(select(AppUser))
    assert user.totp_secret


def test_a_code_sent_anyway_is_checked_not_ignored(client):
    """A client that offers the field must not find it silently doing nothing —
    that is the kind of gap that makes a factor decorative."""
    setup_user(client)
    client.post("/api/auth/logout")

    response = client.post(
        "/api/auth/login",
        json={"username": "ardome", "password": PASSWORD, "code": "000000"},
    )

    assert response.status_code == 401


def test_status_tells_the_screen_not_to_ask_for_a_code(client):
    """So the field is absent rather than present and optional — an input
    nobody has to fill in is an input that reads as a requirement."""
    assert client.get("/api/auth/status").json()["totp_required"] is False
