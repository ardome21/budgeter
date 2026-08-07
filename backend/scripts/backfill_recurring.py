"""Mark transactions recurring where a standing commitment charges for them.

`is_recurring` came across from the workbook's 'Automatic?' column, which was
filled in inconsistently — rent is flagged on 3 of 19 rows. That makes the
committed-vs-flexible split a floor rather than a split, because a charge that
is obviously a standing commitment counts as discretionary spending.

A transaction whose merchant is named by a current fixed cost is safe to mark.

    uv run python scripts/backfill_recurring.py            # report only
    uv run python scripts/backfill_recurring.py --apply    # write it

Dry run by default. This changes how every month's committed figure reads, and
inference over a thousand rows should be looked at before it lands.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend.db import SessionLocal
from backend.models import Transaction
from backend.reconcile import recurring_candidates


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="write the changes (default: report only)"
    )
    args = ap.parse_args()

    with SessionLocal() as session:
        before = session.scalar(
            select(Transaction.id).where(Transaction.is_recurring).limit(1)
        )
        del before

        candidates = recurring_candidates(session)
        if not candidates:
            print("Nothing to change — every matched transaction is already marked.")
            return 0

        by_cost: Counter[str] = Counter()
        for _txn_id, _description, cost in candidates:
            by_cost[cost] += 1

        print(f"{len(candidates)} transactions would be marked recurring:\n")
        for cost, count in by_cost.most_common():
            print(f"  {count:5}  {cost}")

        print("\nExamples:")
        for txn_id, description, cost in candidates[:8]:
            print(f"  #{txn_id:<6} {description[:44]:46} -> {cost}")

        if not args.apply:
            print(
                "\nDry run. Nothing was written. Re-run with --apply to make "
                "these changes."
            )
            return 0

        session.execute(
            update(Transaction)
            .where(Transaction.id.in_([t for t, _, _ in candidates]))
            .values(is_recurring=True)
        )
        session.commit()
        print(f"\nMarked {len(candidates)} transactions as recurring.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
