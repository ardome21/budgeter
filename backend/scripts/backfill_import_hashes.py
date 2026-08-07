"""Give the imported workbook history the content hash it never had.

The one-shot workbook import wrote no `import_hash`, so all 1,291 rows carry
NULL. Dedup matches on that column, which means a bank export covering any of
the same period imports clean as "new" — the preview says so, because a NULL
can never match. The workbook runs to within days of today, so every export
overlaps.

Hashing what is already on file closes the exact-match half of that. It will
not catch everything: the workbook's descriptions were typed by hand ('Kanna',
'Breakfast') and hash differently from the bank's own descriptor for the same
charge. The near-duplicate check on the preview screen — same amount, within
three days — is what catches those. This handles the rows where the wording
does line up, and costs nothing.

Repeats are numbered in id order, the same way the CSV parser numbers them
within a file, so genuinely identical rows each get their own hash instead of
colliding against the unique index.

    uv run python scripts/backfill_import_hashes.py            # report only
    uv run python scripts/backfill_import_hashes.py --apply    # write it

Dry run by default.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend.csv_import import hash_key, row_hash
from backend.db import SessionLocal
from backend.models import Transaction


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="write the changes (default: report only)"
    )
    args = ap.parse_args()

    with SessionLocal() as session:
        rows = session.execute(
            select(
                Transaction.id,
                Transaction.occurred_on,
                Transaction.raw_description,
                Transaction.amount,
                Transaction.account_id,
            )
            .where(Transaction.import_hash.is_(None))
            .order_by(Transaction.id)
        ).all()

        if not rows:
            print("Every transaction already has an import hash.")
            return 0

        repeats: Counter[tuple[str, str, str]] = Counter()
        updates: list[dict[str, object]] = []
        repeated = 0

        for txn_id, occurred_on, description, amount, account_id in rows:
            key = hash_key(occurred_on, description, amount)
            occurrence = repeats[key]
            repeats[key] += 1
            if occurrence:
                repeated += 1
            updates.append(
                {
                    "txn_id": txn_id,
                    "digest": row_hash(
                        occurred_on,
                        description,
                        amount,
                        account_id=account_id,
                        occurrence=occurrence,
                    ),
                }
            )

        # A hash that already exists means the same charge is on file twice
        # under different ids — worth knowing about, and a reason to stop
        # rather than trip the unique index halfway through.
        digests = [u["digest"] for u in updates]
        if len(set(digests)) != len(digests):
            print("Refusing to write: two rows hashed the same. This is a bug.")
            return 1

        clashes = set(
            session.scalars(
                select(Transaction.import_hash).where(
                    Transaction.import_hash.in_(digests)
                )
            ).all()
        )
        if clashes:
            print(
                f"Refusing to write: {len(clashes)} hashes already exist on other rows."
            )
            return 1

        print(f"{len(updates)} transactions would be hashed.")
        if repeated:
            print(
                f"  {repeated} of them repeat an earlier row exactly and are "
                "numbered rather than collapsed."
            )
        print("\nExamples:")
        for row, upd in list(zip(rows, updates, strict=True))[:8]:
            print(f"  #{row.id:<6} {row.raw_description[:40]:42} {upd['digest'][:12]}…")

        if not args.apply:
            print("\nDry run. Nothing was written. Re-run with --apply.")
            return 0

        session.execute(
            update(Transaction),
            [{"id": u["txn_id"], "import_hash": u["digest"]} for u in updates],
        )
        session.commit()
        print(f"\nHashed {len(updates)} transactions.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
