"""Passkeys: sign in with Touch ID instead of typing a code.

A passkey is multi-factor by itself. The private key never leaves the device
(what you have) and the authenticator refuses to use it without a fingerprint
or the device password (what you are, or know) — which is why signing in with
one asks for nothing else. Requiring a password in front of it would be
theatre, not a third factor.

Password and TOTP stay as the fallback, for a machine with no authenticator
and for the day a device is lost.

The challenge lives in the signed session cookie between the two halves of each
ceremony. It has to be per-attempt and unguessable, and the session is already
both of those; a table would add a row to clean up and buy nothing.
"""

import json
from datetime import UTC, datetime

import webauthn
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..auth import get_user, record_success, session_user, start_session
from ..config import settings
from ..db import get_session
from ..models import AppUser, Passkey

router = APIRouter(prefix="/auth/passkeys", tags=["auth"])

REGISTER_CHALLENGE = "passkey_register_challenge"
LOGIN_CHALLENGE = "passkey_login_challenge"


class PasskeyOut(BaseModel):
    id: int
    label: str
    created_at: datetime
    last_used_at: datetime | None


class RegisterFinishIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    # The browser's PublicKeyCredential, JSON-serialised.
    credential: dict


class LoginFinishIn(BaseModel):
    credential: dict


class LoginOut(BaseModel):
    username: str


def _require_session(request: Request, session: Session) -> AppUser:
    user = session_user(request, session)
    if user is None:
        raise HTTPException(401, "sign in to continue")
    return user


@router.get("", response_model=list[PasskeyOut])
def list_passkeys(request: Request, session: Session = Depends(get_session)):
    user = _require_session(request, session)
    return [
        PasskeyOut(
            id=k.id,
            label=k.label,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in session.scalars(
            select(Passkey).where(Passkey.user_id == user.id).order_by(Passkey.id)
        )
    ]


@router.post("/register/begin")
def register_begin(request: Request, session: Session = Depends(get_session)):
    """Options for creating a passkey. Requires an existing session.

    Registering is deliberately not available to a signed-out visitor: adding
    one is equivalent to setting a new credential, so it has to be behind the
    login it will later replace.
    """
    user = _require_session(request, session)

    options = webauthn.generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=str(user.id).encode(),
        user_name=user.username,
        user_display_name=user.username,
        # Excluding what is already registered stops the same authenticator
        # quietly registering itself twice and looking like two devices.
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
            for cid in session.scalars(
                select(Passkey.credential_id).where(Passkey.user_id == user.id)
            )
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Discoverable, so signing in needs no username first — the whole
            # point is one gesture.
            resident_key=ResidentKeyRequirement.REQUIRED,
            # The fingerprint or device password. Without this a passkey is a
            # single factor and would not be worth swapping the code for.
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    request.session[REGISTER_CHALLENGE] = webauthn.helpers.bytes_to_base64url(
        options.challenge
    )
    return json.loads(webauthn.options_to_json(options))


@router.post("/register/finish", response_model=PasskeyOut)
def register_finish(
    payload: RegisterFinishIn,
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_session(request, session)
    challenge = request.session.pop(REGISTER_CHALLENGE, None)
    if not challenge:
        raise HTTPException(400, "no registration in progress — start again")

    try:
        verified = webauthn.verify_registration_response(
            credential=payload.credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origins,
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as exc:
        raise HTTPException(400, f"could not register that passkey: {exc}") from exc

    credential_id = webauthn.helpers.bytes_to_base64url(verified.credential_id)
    if session.scalar(
        select(Passkey).where(Passkey.credential_id == credential_id)
    ):
        raise HTTPException(409, "that passkey is already registered")

    passkey = Passkey(
        user_id=user.id,
        credential_id=credential_id,
        public_key=webauthn.helpers.bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        label=payload.label.strip(),
        transports=",".join(payload.credential.get("transports") or []) or None,
        created_at=datetime.now(UTC),
    )
    session.add(passkey)
    session.commit()
    session.refresh(passkey)
    return PasskeyOut(
        id=passkey.id,
        label=passkey.label,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
    )


@router.post("/login/begin")
def login_begin(request: Request, session: Session = Depends(get_session)):
    """Options for signing in. Public, by necessity."""
    user = get_user(session)
    # Queried rather than read off `user.passkeys`: the collection may already
    # be loaded on the identity-mapped user from earlier in this transaction,
    # and would then answer from a snapshot taken before the passkey existed.
    if user is None or not session.scalar(
        select(Passkey.id).where(Passkey.user_id == user.id).limit(1)
    ):
        raise HTTPException(404, "no passkeys are registered")

    options = webauthn.generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    request.session[LOGIN_CHALLENGE] = webauthn.helpers.bytes_to_base64url(
        options.challenge
    )
    return json.loads(webauthn.options_to_json(options))


@router.post("/login/finish", response_model=LoginOut)
def login_finish(
    payload: LoginFinishIn,
    request: Request,
    session: Session = Depends(get_session),
):
    challenge = request.session.pop(LOGIN_CHALLENGE, None)
    if not challenge:
        raise HTTPException(400, "no sign-in in progress — start again")

    raw_id = payload.credential.get("id")
    passkey = session.scalar(select(Passkey).where(Passkey.credential_id == raw_id))
    if passkey is None:
        raise HTTPException(401, "that passkey is not registered here")

    try:
        verified = webauthn.verify_authentication_response(
            credential=payload.credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origins,
            credential_public_key=base64url_to_bytes(passkey.public_key),
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as exc:
        raise HTTPException(401, f"that did not verify: {exc}") from exc

    # The one attack this protocol can actually detect. A counter that has not
    # advanced past what we last saw means two authenticators are answering for
    # one credential, so the key has been copied.
    if verified.new_sign_count and verified.new_sign_count <= passkey.sign_count:
        raise HTTPException(
            401, "this passkey looks cloned — remove it and register a new one"
        )

    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = datetime.now(UTC)

    user = session.get(AppUser, passkey.user_id)
    record_success(session, user)
    start_session(request, user)
    return LoginOut(username=user.username)


@router.delete("/{passkey_id}")
def remove(
    passkey_id: int, request: Request, session: Session = Depends(get_session)
):
    """Revoke one. The password and code remain, so this cannot lock anyone out."""
    user = _require_session(request, session)
    passkey = session.get(Passkey, passkey_id)
    if passkey is None or passkey.user_id != user.id:
        raise HTTPException(404, "no such passkey")
    label = passkey.label
    session.delete(passkey)
    session.commit()
    return {"removed": label}
