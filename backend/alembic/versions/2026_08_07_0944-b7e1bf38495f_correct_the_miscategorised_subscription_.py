"""correct the miscategorised subscription charge

A Charlotte Observer charge of $28.94 on 2024-05-15 was typed into the workbook
as ' Subscription' — leading space, singular. The sheet's own SUMIF missed it
for exactly that reason, under-reporting May 2024, and the import then repeated
the mistake in a different place by filing it under Misc.

The importer now aliases the misspelling, so a re-import is right. This moves
the row that already exists, because the database is the source of truth and
re-importing is no longer the way corrections land.

Keyed on the source cell rather than a row id, so it is stable and applies
once. Idempotent: if the category is already right, nothing matches.

Revision ID: b7e1bf38495f
Revises: 5bfe49e20056
Create Date: 2026-08-07 09:44:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7e1bf38495f"
down_revision: str | Sequence[str] | None = "5bfe49e20056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_CELL = "Budget 2024!May Spending!r37"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE transactions
           SET category_id = (SELECT id FROM categories WHERE name = 'Subscriptions')
         WHERE source_ref = '{SOURCE_CELL}'
           AND category_id = (SELECT id FROM categories WHERE name = 'Misc')
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE transactions
           SET category_id = (SELECT id FROM categories WHERE name = 'Misc')
         WHERE source_ref = '{SOURCE_CELL}'
           AND category_id = (SELECT id FROM categories WHERE name = 'Subscriptions')
        """
    )
