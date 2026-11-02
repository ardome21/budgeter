"""Refresh every bank without opening the app.

The same pass as the Refresh button — balances, holdings and transactions,
written as they arrive — for a first pull after a rewind, or for a schedule.

    uv run python scripts/refresh.py

Docker has to be up, because the database lives there. Under launchd or cron
that is a real condition and not a given: the job is a no-op that says so
rather than a crash, so a laptop that was asleep produces a log line instead of
a mail spool full of tracebacks.
"""

from __future__ import annotations

import sys

from sqlalchemy.exc import OperationalError

from backend.config import settings
from backend.db import SessionLocal
from backend.routers.plaid_link import refresh_everything


def main() -> int:
    if not settings.plaid_configured:
        print("Plaid is not configured — nothing to refresh.")
        return 0

    try:
        with SessionLocal() as session:
            result = refresh_everything(session)
    except OperationalError:
        print("database unreachable — is Docker running?")
        return 1

    print(
        f"{result.added} added, {result.modified} revised, {result.removed} "
        f"removed; {result.accounts_updated} balances, "
        f"{result.holdings_updated} holdings."
    )
    for failure in result.failures:
        print(failure, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
