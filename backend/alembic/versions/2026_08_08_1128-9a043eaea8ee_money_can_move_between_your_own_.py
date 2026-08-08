"""Money can move between your own accounts, and arrive

Two new category kinds, because linking a *checking* account brought in rows
the workbook never had. It only ever recorded spending, so nothing before this
needed a way to say "this is not an outflow".

Paying a credit card off is the case that forced it: the payment leaves
checking and the card's own purchases arrive separately, so counting the
payment as spending counts the same money twice. On the first day of real data
that was $207.45 of one August — 8% of the month.

`plaid_accounts.account_type` decides two things that must not be guessed:
whether a balance is a liability (stored negative, as the student loan and the
old credit-card row already are), and whether a negative amount is a deposit or
a refund. It is left null here and filled in on the next refresh, since the
value can only come from Plaid.

Revision ID: 9a043eaea8ee
Revises: a036a91de122
Create Date: 2026-08-08 11:28:57.089404

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9a043eaea8ee'
down_revision: str | Sequence[str] | None = 'a036a91de122'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate does not see enum changes, so these are written by hand.
    # Postgres will not let a new label be *used* in the transaction that
    # created it; nothing here uses them, but committing keeps that true for
    # anything added below later. IF NOT EXISTS makes a re-run harmless.
    op.execute("ALTER TYPE category_kind ADD VALUE IF NOT EXISTS 'TRANSFER'")
    op.execute("ALTER TYPE category_kind ADD VALUE IF NOT EXISTS 'INCOME'")
    op.execute("COMMIT")

    op.add_column(
        'plaid_accounts',
        sa.Column('account_type', sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('plaid_accounts', 'account_type')

    # Postgres cannot drop a label from an enum, so the type is rebuilt without
    # them. Refuse rather than guess if any category still claims one: moving a
    # transfer or a paycheck back to SPENDING would silently re-introduce the
    # double-count this migration exists to remove.
    bind = op.get_bind()
    claimed = bind.scalar(
        sa.text(
            "select count(*) from categories where kind in ('TRANSFER', 'INCOME')"
        )
    )
    if claimed:
        raise RuntimeError(
            f"{claimed} categories are still TRANSFER or INCOME. Re-file or "
            "delete them before downgrading — this migration will not reassign "
            "them to SPENDING, which is what caused the double-count."
        )
    op.execute("ALTER TYPE category_kind RENAME TO category_kind_old")
    op.execute("CREATE TYPE category_kind AS ENUM ('SPENDING', 'SAVINGS', 'OTHER')")
    op.execute(
        "ALTER TABLE categories ALTER COLUMN kind TYPE category_kind "
        "USING kind::text::category_kind"
    )
    op.execute("DROP TYPE category_kind_old")
