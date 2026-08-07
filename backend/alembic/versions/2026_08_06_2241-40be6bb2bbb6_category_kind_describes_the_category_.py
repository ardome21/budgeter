"""category kind describes the category not its commitment

COMMITTED/FLEXIBLE was the wrong axis. Whether spending is committed is a
property of the transaction, not of its category — Self Care holds an $89/month
gym membership alongside 77 one-off purchases, and every committed-looking
category in the history has discretionary rows in it. The split now derives
from transactions.is_recurring and the fixed-cost list.

What the enum encodes instead is whether the money actually left: SAVINGS is
moved, not spent, and summing it into a spending total overstates spending.

Written by hand — autogenerate does not detect changes to enum values.

Revision ID: 40be6bb2bbb6
Revises: 5d308f00a86a
Create Date: 2026-08-06 22:41:14.253660

"""

from collections.abc import Sequence

from alembic import op

revision: str = "40be6bb2bbb6"
down_revision: str | Sequence[str] | None = "5d308f00a86a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("COMMITTED", "FLEXIBLE", "SAVINGS", "OTHER")
NEW = ("SPENDING", "SAVINGS", "OTHER")


def upgrade() -> None:
    """COMMITTED and FLEXIBLE both collapse to SPENDING."""
    op.execute("ALTER TYPE category_kind RENAME TO category_kind_old")
    op.execute(f"CREATE TYPE category_kind AS ENUM {NEW!r}")
    op.execute(
        """
        ALTER TABLE categories
        ALTER COLUMN kind TYPE category_kind
        USING (
            CASE kind::text
                WHEN 'COMMITTED' THEN 'SPENDING'
                WHEN 'FLEXIBLE'  THEN 'SPENDING'
                ELSE kind::text
            END
        )::category_kind
        """
    )
    op.execute("DROP TYPE category_kind_old")


def downgrade() -> None:
    """Lossy: SPENDING cannot be split back into COMMITTED and FLEXIBLE.

    Every SPENDING category becomes FLEXIBLE, which is what the majority were.
    Re-run the importer afterwards if the old split matters.
    """
    op.execute("ALTER TYPE category_kind RENAME TO category_kind_new")
    op.execute(f"CREATE TYPE category_kind AS ENUM {OLD!r}")
    op.execute(
        """
        ALTER TABLE categories
        ALTER COLUMN kind TYPE category_kind
        USING (
            CASE kind::text
                WHEN 'SPENDING' THEN 'FLEXIBLE'
                ELSE kind::text
            END
        )::category_kind
        """
    )
    op.execute("DROP TYPE category_kind_new")
