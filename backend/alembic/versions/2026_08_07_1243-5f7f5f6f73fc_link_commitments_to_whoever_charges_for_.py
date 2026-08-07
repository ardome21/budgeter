"""link commitments to whoever charges for them

Reconciliation matched 9 of 16 commitments. The other seven were not drifting,
they were pointing at merchants that stopped being charged: the workbook typed
'Phone' and 'NYT' by hand for three years, and the bank writes 'Spectrum
Mobile' and 'NYTIMES*'. A commitment linked to a name nobody bills under
reports 'expected but not charged this month' forever, which reads like a
missed payment and is not one.

Every link below is confirmed by an exact amount match in July 2026, so this is
a correction rather than a guess:

    Rent            1553.37 expected   BILT CARD HOUSING   1535.17 charged
    Phone             67.49            Spectrum Mobile       67.49
    NYT               31.94            NYTIMES*              31.94
    The Observer      27.05            CHARLOTTE OBSERVER    27.05
    HBO Max           11.89            HELP.HBOMAX.COM       11.89

Rent is linked to the card the rent is paid through, which is what the account
actually sees. Its 18.20 gap against the expected 1553.37 is the drift the
workbook could not see and reconciliation exists to show — not something to
tidy away by picking a merchant that makes the number match.

iCloud is deliberately left unlinked. It bills as APPLE.COM/BILL, the same
descriptor as Apple TV, so both resolve to one merchant and no link can tell
0.99 of iCloud from 12.99 of Apple TV. Apple TV therefore reports a 0.99 drift
that is really the iCloud charge. An honest unmatched row beats a link that
quietly reports one subscription's cost as another's.

Revision ID: 5f7f5f6f73fc
Revises: a29f71998cc3
Create Date: 2026-08-07 12:43:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "5f7f5f6f73fc"
down_revision: str | Sequence[str] | None = "a29f71998cc3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# fixed cost description -> the merchant that actually charges for it
LINKS = {
    "Rent (billed as one charge)": "Bilt Card Housing",
    "Phone": "Spectrum",
    "NYT": "Nytimes",
    "HBO Max": "Help Hbomax Com",
    "The Observer": "Charlotte Observer",
    "Energy": "Duke Energy",
}


def upgrade() -> None:
    for description, merchant in LINKS.items():
        # Only current rows. A commitment that has already ended was linked
        # correctly for the period it covered, and rewriting it would change
        # what last year reconciled to.
        op.execute(
            f"""
            UPDATE fixed_costs
               SET merchant_id = (
                   SELECT id FROM merchants WHERE canonical_name = '{merchant}'
               )
             WHERE description = '{description}'
               AND effective_to IS NULL
               AND EXISTS (
                   SELECT 1 FROM merchants WHERE canonical_name = '{merchant}'
               )
            """
        )


def downgrade() -> None:
    names = "', '".join(LINKS)
    op.execute(
        f"""
        UPDATE fixed_costs SET merchant_id = NULL
         WHERE description IN ('{names}') AND effective_to IS NULL
        """
    )
