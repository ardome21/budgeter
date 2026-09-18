"""The starting set of categories.

Carried over from the schema this replaced, because they are the only part of
the old configuration that was genuinely the user's rather than the workbook's
— three years of filing decisions, and every merchant suggestion depends on
matching a name that is already in use.

Two changes. `Deficit Reduction` is gone: it was `OTHER`, a kind that no longer
exists, and it described a workbook row rather than a place money went.
`Savings` becomes SAVINGS rather than a spending category, so moving money into
it stops counting against the month.

Idempotent — an existing name is left exactly as it is, so running this twice
cannot renumber or re-kind a category somebody has since edited.
"""

from __future__ import annotations

from sqlalchemy import func, select

from backend.db import SessionLocal
from backend.models import Category, CategoryKind

CATEGORIES: list[tuple[str, CategoryKind]] = [
    ("Income", CategoryKind.INCOME),
    ("Savings", CategoryKind.SAVINGS),
    ("Transfer", CategoryKind.TRANSFER),
    ("Rent", CategoryKind.SPENDING),
    ("Utilities", CategoryKind.SPENDING),
    ("Insurance", CategoryKind.SPENDING),
    ("Loan Payments", CategoryKind.SPENDING),
    ("Groceries", CategoryKind.SPENDING),
    ("Food and Drinks", CategoryKind.SPENDING),
    ("Transportation", CategoryKind.SPENDING),
    ("Subscriptions", CategoryKind.SPENDING),
    ("Shopping", CategoryKind.SPENDING),
    ("Entertainment", CategoryKind.SPENDING),
    ("Travel", CategoryKind.SPENDING),
    ("Self Care", CategoryKind.SPENDING),
    ("Donations", CategoryKind.SPENDING),
    ("Misc", CategoryKind.SPENDING),
]


def main() -> int:
    with SessionLocal() as session:
        added = 0
        for order, (name, kind) in enumerate(CATEGORIES):
            existing = session.scalar(
                select(Category).where(func.lower(Category.name) == name.lower())
            )
            if existing is not None:
                continue
            session.add(Category(name=name, kind=kind, sort_order=order))
            added += 1
        session.commit()

    print(f"{added} categories added, {len(CATEGORIES) - added} already present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
