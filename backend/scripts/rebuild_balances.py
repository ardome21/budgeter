"""Fill the net-worth series in behind the readings. See `backend.rebuild`.

Safe to run any time: it deletes and recomputes only the rows it wrote, and a
reading — from the bank or the archive — is never touched.

    uv run python scripts/rebuild_balances.py
"""

from __future__ import annotations

from backend.db import SessionLocal
from backend.rebuild import rebuild_balances


def main() -> int:
    with SessionLocal() as session:
        written = rebuild_balances(session)
    print(f"{written} balances rebuilt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
