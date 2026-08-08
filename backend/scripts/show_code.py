"""Print the current six-digit code, for when the authenticator isn't set up yet.

A stopgap, and worth being honest about what it costs: anyone who can run this
already has the database and the encryption key, so it hands them the second
factor too. It is no weaker than `reset_auth.py`, which sitting beside it can
delete the login outright — both need shell access to the machine holding the
data, which is a higher bar than the login itself asks for.

It exists because the gap between "set up the login" and "have an authenticator
that works" is real, and being locked out of your own budget in the middle of
it is worse than this script existing.

**The fix is to stop needing it.** Either:

  - register a passkey (Settings → Signing in) and never type a code again, or
  - add the setup key it prints to any authenticator app.

    uv run python scripts/show_code.py
    uv run python scripts/show_code.py --setup-key   # also print the secret
"""

import argparse
import sys
import time

import pyotp
from sqlalchemy import select

from backend.db import SessionLocal
from backend.models import AppUser
from backend.plaid_client import decrypt_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the current TOTP code.")
    parser.add_argument(
        "--setup-key",
        action="store_true",
        help="also print the secret, to add to an authenticator by hand",
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        user = session.scalars(select(AppUser)).first()
        if user is None:
            print("No user is set up. Open the app and run setup.")
            return 1
        if not user.totp_confirmed:
            print(f"{user.username}'s enrolment was never confirmed.")
            print("Finish it on the setup screen with the code below.")
        secret = decrypt_token(user.totp_secret)

    totp = pyotp.TOTP(secret)
    remaining = int(30 - (time.time() % 30))

    print(f"user: {user.username}")
    print()
    # The next code is printed too, because a code with three seconds left is
    # a code that will be rejected by the time it is typed.
    print(f"   CODE: {totp.now()}   ({remaining}s left)")
    print(f"   NEXT: {totp.at(time.time() + 30)}")

    if args.setup_key:
        print()
        print(f"setup key: {secret}")
        print("Add that to an authenticator app, then stop using this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
