"""Link accounts to the bank instead of exporting CSVs

Adds the two tables behind a linked institution, a fourth transaction source,
and the index that makes a bank's own transaction id an identity.

`source_ref` already existed as workbook provenance — the cell a figure came
from. For a LINKED row it carries Plaid's transaction id instead, which is why
the unique index is partial: the column means different things on either side
of `source`, and only one of them is a key.

Revision ID: 7c1a804c3075
Revises: c4d1e8b90a11
Create Date: 2026-08-07 23:53:13.432539

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7c1a804c3075'
down_revision: str | Sequence[str] | None = 'c4d1e8b90a11'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres will not let a new enum label be *used* in the transaction that
    # created it, and the partial index below uses it. Committing here is what
    # makes that possible; IF NOT EXISTS keeps a re-run after a later failure
    # harmless.
    op.execute("ALTER TYPE transaction_source ADD VALUE IF NOT EXISTS 'LINKED'")
    op.execute("COMMIT")

    op.create_table(
        'plaid_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.String(length=120), nullable=False),
        sa.Column('institution_id', sa.String(length=60), nullable=True),
        sa.Column('institution_name', sa.String(length=60), nullable=False),
        # Fernet ciphertext. Never the raw token — it is a long-lived read key
        # to a real bank account and this database gets dumped like any other.
        sa.Column('access_token', sa.Text(), nullable=False),
        sa.Column('cursor', sa.Text(), nullable=True),
        sa.Column('pending_cursor', sa.Text(), nullable=True),
        sa.Column('sync_start_on', sa.Date(), nullable=False),
        sa.Column('needs_reauth', sa.Boolean(), nullable=False),
        sa.Column('linked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_plaid_items')),
        sa.UniqueConstraint('item_id', name=op.f('uq_plaid_items_item_id')),
    )
    op.create_table(
        'plaid_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('plaid_account_id', sa.String(length=120), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('mask', sa.String(length=10), nullable=True),
        sa.Column('subtype', sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(
            ['account_id'], ['accounts.id'],
            name=op.f('fk_plaid_accounts_account_id_accounts'),
        ),
        sa.ForeignKeyConstraint(
            ['item_id'], ['plaid_items.id'],
            name=op.f('fk_plaid_accounts_item_id_plaid_items'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_plaid_accounts')),
        sa.UniqueConstraint(
            'plaid_account_id', name=op.f('uq_plaid_accounts_plaid_account_id')
        ),
    )
    op.create_index(
        'uq_transactions_linked_source_ref',
        'transactions',
        ['source_ref'],
        unique=True,
        postgresql_where=sa.text("source = 'LINKED'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'uq_transactions_linked_source_ref',
        table_name='transactions',
        postgresql_where=sa.text("source = 'LINKED'"),
    )
    op.drop_table('plaid_accounts')
    op.drop_table('plaid_items')

    # Postgres cannot drop a label from an enum, so the type is rebuilt without
    # it. Refuse rather than guess if any row still claims the label: those are
    # real transactions and picking a different source for them silently would
    # misattribute where the user's money came from.
    linked = op.get_bind().scalar(
        sa.text("select count(*) from transactions where source = 'LINKED'")
    )
    if linked:
        raise RuntimeError(
            f"{linked} transactions still have source = LINKED. Re-file or delete "
            "them before downgrading — this migration will not reassign them."
        )
    op.execute("ALTER TYPE transaction_source RENAME TO transaction_source_old")
    op.execute(
        "CREATE TYPE transaction_source AS ENUM ('WORKBOOK', 'CSV', 'MANUAL')"
    )
    op.execute(
        "ALTER TABLE transactions ALTER COLUMN source TYPE transaction_source "
        "USING source::text::transaction_source"
    )
    op.execute("DROP TYPE transaction_source_old")
