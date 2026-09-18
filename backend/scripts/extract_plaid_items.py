"""Lift the Plaid items out of the database before the schema is replaced.

The access tokens are the only thing here that cannot be rebuilt. The code can
be rewritten and the transactions can be re-fetched, but an access token is
issued once, per item, and the account has a finite lifetime supply of items —
re-linking five banks to recover from a dropped table is a real cost.

Writes plaintext tokens, on purpose: the file has to survive a change to
PLAID_TOKEN_KEY, which is exactly one of the things a rebuild might do. Keep it
out of the repo and delete it once the restore is confirmed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import text

from backend.db import SessionLocal
from backend.plaid_client import decrypt_token


def main(destination: Path) -> int:
    with SessionLocal() as session:
        rows = session.execute(
            text(
                "select id, item_id, institution_id, institution_name,"
                " access_token, sync_start_on, linked_at"
                " from plaid_items order by id"
            )
        ).mappings().all()

    items = []
    for row in rows:
        items.append(
            {
                "old_id": row["id"],
                "item_id": row["item_id"],
                "institution_id": row["institution_id"],
                "institution_name": row["institution_name"],
                "access_token": decrypt_token(row["access_token"]),
                "sync_start_on": str(row["sync_start_on"]),
                "linked_at": row["linked_at"].isoformat(),
            }
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(items, indent=2) + "\n")
    destination.chmod(0o600)

    for item in items:
        token = item["access_token"]
        print(f"  {item['institution_name']:<18} {token[:12]}…{token[-4:]}")
    print(f"\n{len(items)} items -> {destination}")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]).expanduser()
    raise SystemExit(main(target))
