"""a merchant is a name on a row, not a table

The three merchant tables existed to solve one problem: many bank strings, one
place. They solved it *after the fact* — resolve on write, then let a human
merge the leftovers. That produced 403 merchants from 1,291 transactions, 278
of them used exactly once, and a review queue nobody could empty.

The queue was never the real cost. The real cost is that the merge queue is the
only place a mistake can be corrected, so every mistake has to be made first.
Choosing the merchant *at entry* — from a list of what already exists — means
the strings never diverge and there is nothing to merge later.

So `transactions.merchant_id` and `fixed_costs.merchant_id` become
`merchant_key`: the name itself, on the row. Grouping is by string equality.

**Seeded from `merchants.canonical_name`, deliberately, not by re-normalizing
the descriptions.** 58 merchants are built from more than one normalized
pattern and they carry 768 of the 1,291 transactions — Harris Teeter is three
patterns, Uber is eight, Lyft is eight. Re-normalizing would shatter exactly
the identities that matter and silently degrade the category history that
import inference reads from. Taking the canonical name keeps every one of the
96 merges already made by hand.

What is genuinely lost: there is no longer a UI to merge two keys. When the
normalizer mis-reads a new bank descriptor the fix is to correct the merchant
on the row, or to fix `normalize_merchant`. That is the trade the queue's
removal buys.

Revision ID: c4d1e8b90a11
Revises: 5f7f5f6f73fc
Create Date: 2026-08-07 22:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d1e8b90a11"
down_revision: str | Sequence[str] | None = "5f7f5f6f73fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions", sa.Column("merchant_key", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "fixed_costs", sa.Column("merchant_key", sa.String(length=120), nullable=True)
    )

    # The canonical name, not a re-normalization of the description. See the
    # module docstring: 60% of rows belong to a merchant that only exists
    # because several patterns were folded into it.
    op.execute(
        """
        UPDATE transactions t
           SET merchant_key = m.canonical_name
          FROM merchants m
         WHERE m.id = t.merchant_id
        """
    )
    op.execute(
        """
        UPDATE fixed_costs f
           SET merchant_key = m.canonical_name
          FROM merchants m
         WHERE m.id = f.merchant_id
        """
    )

    op.create_index("ix_transactions_merchant_key", "transactions", ["merchant_key"])

    op.drop_constraint(
        "fk_transactions_merchant_id_merchants", "transactions", type_="foreignkey"
    )
    op.drop_column("transactions", "merchant_id")
    op.drop_constraint(
        "fk_fixed_costs_merchant_id_merchants", "fixed_costs", type_="foreignkey"
    )
    op.drop_column("fixed_costs", "merchant_id")

    op.drop_table("merchant_splits")
    op.drop_table("merchant_patterns")
    op.drop_table("merchants")


def downgrade() -> None:
    """Rebuild the tables from the distinct keys.

    Honest about what it cannot restore: patterns. A merchant came back from
    its name, but the several descriptor patterns that used to resolve to it
    are gone, so each rebuilt merchant gets one pattern derived from its name.
    Re-running the upgrade after this is still lossless — the names survive,
    and the names are what the new schema keys on.
    """
    op.create_table(
        "merchants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("canonical_name", sa.String(length=120), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_table(
        "merchant_patterns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=False),
        sa.Column("pattern", sa.String(length=120), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pattern"),
    )
    op.create_table(
        "merchant_splits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("left_name", sa.String(length=120), nullable=False),
        sa.Column("right_name", sa.String(length=120), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("left_name", "right_name"),
    )

    op.execute(
        """
        INSERT INTO merchants (canonical_name)
        SELECT DISTINCT merchant_key FROM transactions WHERE merchant_key IS NOT NULL
        UNION
        SELECT DISTINCT merchant_key FROM fixed_costs WHERE merchant_key IS NOT NULL
        """
    )
    op.execute(
        "INSERT INTO merchant_patterns (merchant_id, pattern) "
        "SELECT id, lower(canonical_name) FROM merchants"
    )

    op.add_column("fixed_costs", sa.Column("merchant_id", sa.Integer(), nullable=True))
    op.add_column("transactions", sa.Column("merchant_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_fixed_costs_merchant_id_merchants",
        "fixed_costs",
        "merchants",
        ["merchant_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_transactions_merchant_id_merchants",
        "transactions",
        "merchants",
        ["merchant_id"],
        ["id"],
    )
    op.execute(
        "UPDATE transactions t SET merchant_id = m.id "
        "FROM merchants m WHERE m.canonical_name = t.merchant_key"
    )
    op.execute(
        "UPDATE fixed_costs f SET merchant_id = m.id "
        "FROM merchants m WHERE m.canonical_name = f.merchant_key"
    )

    op.drop_index("ix_transactions_merchant_key", table_name="transactions")
    op.drop_column("fixed_costs", "merchant_key")
    op.drop_column("transactions", "merchant_key")
