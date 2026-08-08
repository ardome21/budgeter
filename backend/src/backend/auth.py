"""Password, second factor, and the dependency that guards every route.

The app holds three years of financial history and, since linking, long-lived
read tokens to real bank accounts. It previously had no authentication at all,
which was defensible when it was a spreadsheet replacement on loopback and is
not once those tokens exist.

Two factors, because one of them is the point: an attacker who has read the
database has the password hash and the encrypted TOTP secret, and neither gets
them a code. That is why the secret is encrypted rather than stored plainly —
a TOTP secret in the clear makes the second factor decorative against precisely
the attacker it exists to stop.
"""

import secrets
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import AppUser, RecoveryCode
from .plaid_client import decrypt_token, encrypt_token

# Defaults are the argon2-cffi maintained profile; pinning numbers here would
# freeze them at whatever was current the day this was written.
_hasher = PasswordHasher()

ISSUER = "budgeter"
RECOVERY_CODE_COUNT = 10
LOCKOUT = timedelta(minutes=15)

# Reachable without a session. Everything else requires one.
PUBLIC_PATHS = {
    "/api/health",
    "/api/health/db",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/setup/confirm",
    "/api/auth/login",
    "/api/auth/logout",
    # Signing in with a passkey has to be reachable while signed out. The
    # *register* endpoints deliberately are not: adding one sets a credential,
    # so it sits behind the login it will go on to replace.
    "/api/auth/passkeys/login/begin",
    "/api/auth/passkeys/login/finish",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(hashed: str) -> bool:
    """True when argon2-cffi's recommended parameters have moved on."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """The otpauth:// URI an authenticator app scans."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def verify_totp(encrypted_secret: str, code: str) -> bool:
    """Check a six-digit code, allowing one step of clock drift either way."""
    cleaned = (code or "").strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    return pyotp.TOTP(decrypt_token(encrypted_secret)).verify(cleaned, valid_window=1)


def encrypt_secret(secret: str) -> str:
    return encrypt_token(secret)


def new_recovery_codes() -> list[str]:
    """Ten single-use codes, in the format people are used to retyping."""
    return [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(RECOVERY_CODE_COUNT)
    ]


def consume_recovery_code(session: Session, user: AppUser, given: str) -> bool:
    """Spend one unused recovery code, if it matches. Single use, always."""
    candidate = (given or "").strip().lower()
    if not candidate:
        return False
    for code in user.recovery_codes:
        if code.used_at is not None:
            continue
        if verify_password(code.code_hash, candidate):
            code.used_at = datetime.now(UTC)
            session.flush()
            return True
    return False


def get_user(session: Session) -> AppUser | None:
    """The single user, or None before setup has run."""
    return session.scalars(select(AppUser).limit(1)).first()


def is_locked(user: AppUser) -> bool:
    return user.locked_until is not None and user.locked_until > datetime.now(UTC)


def record_failure(session: Session, user: AppUser) -> None:
    """Count a bad attempt, and lock the account once there are too many.

    Slows an offline-guessing attacker who has the database but is trying the
    live endpoint, and makes a locked-out login visible rather than silent.
    """
    user.failed_logins += 1
    if user.failed_logins >= settings.max_failed_logins:
        user.locked_until = datetime.now(UTC) + LOCKOUT
        user.failed_logins = 0
    session.commit()


def record_success(session: Session, user: AppUser) -> None:
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)
    session.commit()


def start_session(request: Request, user: AppUser) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["at"] = datetime.now(UTC).isoformat()


def end_session(request: Request) -> None:
    request.session.clear()


def session_user(request: Request, session: Session) -> AppUser | None:
    """The signed-in user, or None. Expiry is enforced here, not by the cookie.

    A cookie's own max-age is a client-side hint and an attacker replaying a
    stolen cookie simply ignores it, so the issue time is written into the
    signed payload and checked on the server.
    """
    uid = request.session.get("uid")
    issued = request.session.get("at")
    if not uid or not issued:
        return None
    try:
        started = datetime.fromisoformat(issued)
    except ValueError:
        return None
    if datetime.now(UTC) - started > timedelta(days=settings.session_days):
        return None
    return session.get(AppUser, uid)


def hash_recovery_codes(session: Session, user: AppUser, codes: list[str]) -> None:
    """Replace the recovery codes with a fresh hashed set."""
    user.recovery_codes.clear()
    session.flush()
    for code in codes:
        session.add(RecoveryCode(user_id=user.id, code_hash=hash_password(code)))
    session.flush()
