"""Break glass: clear the login so setup can run again.

The last way in when the authenticator is gone *and* the recovery codes are
gone. It deliberately requires shell access to the machine holding the
database, which is a higher bar than anything the login itself asks for — that
is the whole reason it is safe to have.

It removes the user, not the data. Every transaction, account and linked bank
stays exactly where it is; the next visit to the app runs setup again.

A dry run by default, like every script here:

    uv run python scripts/reset_auth.py           # says what it would do
    uv run python scripts/reset_auth.py --apply   # does it
"""

import argparse
import sys

from sqlalchemy import select

from backend.db import SessionLocal
from backend.models import AppUser


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete the user (otherwise this only reports)",
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        users = session.scalars(select(AppUser)).all()
        if not users:
            print("No user is set up. The app will run setup on next visit.")
            return 0

        for user in users:
            unused = sum(1 for c in user.recovery_codes if c.used_at is None)
            print(f"user:            {user.username}")
            print(f"created:         {user.created_at}")
            print(f"last login:      {user.last_login_at or 'never'}")
            print(f"recovery codes:  {unused} unused")
            print(f"second factor:   {'confirmed' if user.totp_confirmed else 'pending'}")

        if not args.apply:
            print()
            print(
                f"Dry run. {len(users)} user(s) would be deleted, and setup would run "
                "again on the next visit."
            )
            print("Re-run with --apply to do it. No transaction data is touched.")
            return 0

        for user in users:
            session.delete(user)
        session.commit()
        print()
        print("Login cleared. Open the app and set it up again.")
        print("Every transaction, account and linked bank is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
