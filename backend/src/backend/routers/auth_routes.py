"""Setting up the login, and using it.

Setup runs once and then closes for good: `/setup` refuses the moment a
confirmed user exists, so the window where the API is open is exactly the
window before anyone has claimed it.

Enrolment is deliberately two steps. Creating the user hands back a secret and
ten recovery codes; the account is not *confirmed* until a code generated from
that secret comes back. Without the second step a mistyped scan would lock the
only user out of their own data on first launch.
"""

import io
from datetime import UTC, datetime

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    consume_recovery_code,
    encrypt_secret,
    end_session,
    get_user,
    hash_password,
    hash_recovery_codes,
    is_locked,
    needs_rehash,
    new_recovery_codes,
    new_totp_secret,
    provisioning_uri,
    record_failure,
    record_success,
    session_user,
    start_session,
    verify_password,
    verify_totp,
)
from ..db import get_session
from ..models import AppUser, Passkey

router = APIRouter(prefix="/auth", tags=["auth"])


class StatusOut(BaseModel):
    # False before setup has run: the browser sends you to /setup rather than
    # to a login that cannot succeed yet.
    configured: bool
    authenticated: bool
    username: str | None
    # Whether to offer the Touch ID button at all. Readable while signed out
    # by necessity — the login screen has to decide before anyone has proved
    # anything, and "a passkey exists here" is not worth protecting.
    has_passkeys: bool = False


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=12, max_length=200)


class SetupOut(BaseModel):
    username: str
    otpauth_uri: str
    qr_svg: str
    # Shown exactly once. There is no endpoint that returns them again, because
    # anything that can re-read them is a second way past the second factor.
    recovery_codes: list[str]


class ConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class LoginIn(BaseModel):
    username: str
    password: str
    # A TOTP code, or one of the recovery codes.
    code: str


class LoginOut(BaseModel):
    username: str


def _qr_svg(uri: str) -> str:
    """The provisioning URI as an inline SVG.

    Rendered here rather than in the browser so the page needs no QR library
    and no external request — the strict thing to avoid is a third party
    seeing a URI that contains the TOTP secret.
    """
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()


@router.get("/status", response_model=StatusOut)
def status(request: Request, session: Session = Depends(get_session)):
    user = get_user(session)
    if user is None or not user.totp_confirmed:
        return StatusOut(configured=False, authenticated=False, username=None)
    current = session_user(request, session)
    return StatusOut(
        configured=True,
        authenticated=current is not None,
        username=current.username if current else None,
        has_passkeys=bool(
            session.scalar(
                select(Passkey.id).where(Passkey.user_id == user.id).limit(1)
            )
        ),
    )


@router.post("/setup", response_model=SetupOut)
def setup(payload: SetupIn, session: Session = Depends(get_session)):
    """Claim the app. Allowed only while no confirmed user exists."""
    existing = get_user(session)
    if existing is not None and existing.totp_confirmed:
        raise HTTPException(409, "this app already has a user — sign in instead")

    if existing is not None:
        # An enrolment that was started and never confirmed. It has never been
        # used for anything, so replacing it is safe and is the only way out of
        # a scan that went wrong.
        session.delete(existing)
        session.flush()

    secret = new_totp_secret()
    user = AppUser(
        username=payload.username.strip(),
        password_hash=hash_password(payload.password),
        totp_secret=encrypt_secret(secret),
        totp_confirmed=False,
        failed_logins=0,
        created_at=datetime.now(UTC),
    )
    session.add(user)
    session.flush()

    codes = new_recovery_codes()
    hash_recovery_codes(session, user, codes)
    session.commit()

    uri = provisioning_uri(secret, user.username)
    return SetupOut(
        username=user.username,
        otpauth_uri=uri,
        qr_svg=_qr_svg(uri),
        recovery_codes=codes,
    )


@router.post("/setup/confirm", response_model=LoginOut)
def confirm(
    payload: ConfirmIn, request: Request, session: Session = Depends(get_session)
):
    """Prove the authenticator was really scanned, then sign in."""
    user = get_user(session)
    if user is None:
        raise HTTPException(409, "start setup first")
    if user.totp_confirmed:
        raise HTTPException(409, "already set up — sign in instead")
    if not verify_totp(user.totp_secret, payload.code):
        raise HTTPException(401, "that code did not match — check the app and retry")

    user.totp_confirmed = True
    record_success(session, user)
    start_session(request, user)
    return LoginOut(username=user.username)


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, request: Request, session: Session = Depends(get_session)):
    user = get_user(session)
    if user is None or not user.totp_confirmed:
        raise HTTPException(409, "this app has no user yet — run setup")

    if is_locked(user):
        raise HTTPException(
            429, "too many failed attempts — wait a few minutes and try again"
        )

    # Password and second factor are checked together and fail together. Saying
    # which one was wrong tells an attacker holding one of them that it works.
    password_ok = verify_password(user.password_hash, payload.password)
    code_ok = verify_totp(user.totp_secret, payload.code) or consume_recovery_code(
        session, user, payload.code
    )

    if not (password_ok and code_ok):
        record_failure(session, user)
        raise HTTPException(401, "that did not match")

    if needs_rehash(user.password_hash):
        # argon2's recommended cost has moved on since this hash was written.
        # Now is the only moment the plaintext is available to upgrade it.
        user.password_hash = hash_password(payload.password)

    record_success(session, user)
    start_session(request, user)
    return LoginOut(username=user.username)


@router.post("/logout")
def logout(request: Request):
    end_session(request)
    return {"signed_out": True}
