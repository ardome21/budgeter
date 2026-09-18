"""Put the Plaid items back after the schema was replaced.

Two things are deliberately *not* restored:

`cursor` comes back NULL. A cursor is a bookmark into a stream this database no
longer has — the rebuild dropped every transaction it had already consumed, so
keeping the bookmark would mean the first refresh returns only what has posted
since, and the twenty-four months of history the whole reset depends on would
never arrive. A null cursor asks Plaid for everything it has.

`sync_start_on` is pushed back to the floor passed on the command line, for the
same reason. It was set to "the day after the newest hand-typed row" when the
point of the floor was to avoid colliding with imported workbook history. That
history is gone, so the floor is now the only thing standing between the app
and a full backfill.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from backend.db import SessionLocal
from backend.models import PlaidItem
from backend.plaid_client import encrypt_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="JSON from extract_plaid_items.py")
    parser.add_argument(
        "--sync-from",
        type=date.fromisoformat,
        required=True,
        help="Floor for the first sync, e.g. 2024-09-18",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write. Without it, nothing is inserted."
    )
    args = parser.parse_args()

    items = json.loads(args.source.read_text())

    with SessionLocal() as session:
        existing = {item.item_id for item in session.query(PlaidItem).all()}

        for entry in items:
            if entry["item_id"] in existing:
                print(f"  {entry['institution_name']:<18} already present, skipped")
                continue

            print(f"  {entry['institution_name']:<18} sync from {args.sync_from}")
            if not args.apply:
                continue

            session.add(
                PlaidItem(
                    item_id=entry["item_id"],
                    institution_id=entry["institution_id"],
                    institution_name=entry["institution_name"],
                    access_token=encrypt_token(entry["access_token"]),
                    products="transactions",
                    cursor=None,
                    pending_cursor=None,
                    sync_start_on=args.sync_from,
                    needs_reauth=False,
                    linked_at=datetime.fromisoformat(entry["linked_at"]),
                    last_synced_at=None,
                )
            )

        if args.apply:
            session.commit()
            print(f"\n{len(items)} items restored.")
        else:
            print(f"\nDry run — {len(items)} items would be restored. Pass --apply.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
