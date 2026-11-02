"""keep pending charges

Revision ID: c3d1f0a2b9e4
Revises: a6a0e24a75e7
Create Date: 2026-11-02 21:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d1f0a2b9e4'
down_revision: str | Sequence[str] | None = 'a6a0e24a75e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'transactions',
        sa.Column('pending', sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Pending rows only exist because of this column; without it they would
    # read as posted spending.
    op.execute("DELETE FROM transactions WHERE pending")
    op.drop_column('transactions', 'pending')
