"""split the merchants that were wrongly fused

The normalizer stripped any run of eight or more capitals as an opaque id.
Descriptors arrive in capitals, so it was eating merchant names: 'DUKEENERGY
BILL PAY 910175813041' became 'bill pay', and 'bill' is one typo from 'bilt',
so the first-word rule offered the power bill and the rent as one merchant and
they were merged. 'PHOTOBOOTH- APPLE INDU' became 'apple indu' and joined
Apple, which is a photo-booth company called Apple Industries, not Apple.

Four names normalized to nothing at all and their transactions got no merchant:
CHARLOTTE OBSERVER, GUESTRS*BELLAGIO, TST* POTBELLY SANDWICH SH, GRANDFATHER
MOUNTAIN LINVILLE NC. The Observer subscription therefore reconciled against
an empty month while the charge sat right there, unattached.

`merchants.py` no longer strips a long run without a digit in it, which fixes
every future import. This repairs the rows already imported.

Deliberately narrow. The history is *not* re-resolved wholesale: the old
normalizer was lossy in a way that happened to unify merchants correctly in
other places — every KANNA descriptor collapsed to 'kanna', every Claude one to
'claude sub' — so re-running it would fragment those and discard hand-merges.
Only the descriptors that were provably fused or dropped are touched.

`Rent` and `Bilt Card Housing` are left as two merchants on purpose. Whether a
hand-typed workbook label and a rent-payment card are one payee across an
apartment move is a judgement about the money, not a normalizer defect, and the
merchant workbench is where that call gets made.

Revision ID: a29f71998cc3
Revises: 27022c3157a8
Create Date: 2026-08-07 12:42:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a29f71998cc3"
down_revision: str | Sequence[str] | None = "27022c3157a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (descriptor, merchant to own it, pattern that should resolve to it)
REHOMED = [
    # Split out of 'Bill Pay', which fused the two.
    ("BILT CARD HOUSING", "Bilt Card Housing", "bilt card housing"),
    ("DUKEENERGY BILL PAY 910175813041", "Duke Energy", "dukeenergy bill pay"),
    # Apple Industries, the photo-booth company. Not Apple.
    (
        "PHOTOBOOTH- APPLE INDU 5166198000 NY",
        "Photobooth Apple Indu",
        "photobooth apple indu",
    ),
    # These had no merchant at all: their descriptors normalized to "".
    ("CHARLOTTE OBSERVER", "Charlotte Observer", "observer"),
    ("GUESTRS*BELLAGIO", "Bellagio", "bellagio"),
    ("TST* POTBELLY SANDWICH SH", "Potbelly Sandwich", "potbelly sandwich"),
    (
        "GRANDFATHER MOUNTAIN LINVILLE NC",
        "Grandfather Mountain Linville",
        "grandfather mountain linville",
    ),
]

# Patterns the old normalizer produced that must not resolve anything now:
# 'bill pay' is a payment rail rather than a payee, and 'apple indu' is the
# truncation that put the photo booth inside Apple.
DEAD_PATTERNS = ["bill pay", "apple indu"]


def upgrade() -> None:
    for descriptor, merchant, pattern in REHOMED:
        op.execute(
            f"""
            INSERT INTO merchants (canonical_name) VALUES ('{merchant}')
            ON CONFLICT (canonical_name) DO NOTHING
            """
        )
        op.execute(
            f"""
            INSERT INTO merchant_patterns (merchant_id, pattern)
            SELECT id, '{pattern}' FROM merchants WHERE canonical_name = '{merchant}'
            ON CONFLICT (pattern) DO NOTHING
            """
        )
        op.execute(
            f"""
            UPDATE transactions
               SET merchant_id = (
                   SELECT id FROM merchants WHERE canonical_name = '{merchant}'
               )
             WHERE raw_description = '{descriptor}'
            """
        )

    for pattern in DEAD_PATTERNS:
        op.execute(f"DELETE FROM merchant_patterns WHERE pattern = '{pattern}'")

    # 'Bill Pay' now owns nothing. Anything still pointing at it would be a
    # descriptor this migration did not anticipate, so it is left alone and the
    # merchant survives — a silent reassignment is how the fusion happened.
    op.execute(
        """
        DELETE FROM merchants
         WHERE canonical_name = 'Bill Pay'
           AND NOT EXISTS (
               SELECT 1 FROM transactions t WHERE t.merchant_id = merchants.id
           )
           AND NOT EXISTS (
               SELECT 1 FROM merchant_patterns p WHERE p.merchant_id = merchants.id
           )
        """
    )


def downgrade() -> None:
    """Put the two fused merchants back, but not the dropped names.

    Restoring 'Bill Pay' means restoring the fusion, because that is what it
    was. The four transactions that had no merchant are left attached: nothing
    was wrong with them that going back would fix.
    """
    op.execute(
        """
        INSERT INTO merchants (canonical_name) VALUES ('Bill Pay')
        ON CONFLICT (canonical_name) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO merchant_patterns (merchant_id, pattern)
        SELECT id, 'bill pay' FROM merchants WHERE canonical_name = 'Bill Pay'
        ON CONFLICT (pattern) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE transactions
           SET merchant_id = (SELECT id FROM merchants WHERE canonical_name = 'Bill Pay')
         WHERE raw_description IN (
             'BILT CARD HOUSING', 'DUKEENERGY BILL PAY 910175813041'
         )
        """
    )
    op.execute(
        """
        UPDATE transactions
           SET merchant_id = (SELECT id FROM merchants WHERE canonical_name = 'Apple')
         WHERE raw_description = 'PHOTOBOOTH- APPLE INDU 5166198000 NY'
        """
    )
    op.execute(
        """
        INSERT INTO merchant_patterns (merchant_id, pattern)
        SELECT id, 'apple indu' FROM merchants WHERE canonical_name = 'Apple'
        ON CONFLICT (pattern) DO NOTHING
        """
    )
    op.execute(
        """
        DELETE FROM merchant_patterns
         WHERE pattern IN ('bilt card housing', 'dukeenergy bill pay',
                           'photobooth apple indu')
        """
    )
    op.execute(
        """
        DELETE FROM merchants
         WHERE canonical_name IN ('Bilt Card Housing', 'Photobooth Apple Indu')
           AND NOT EXISTS (
               SELECT 1 FROM transactions t WHERE t.merchant_id = merchants.id
           )
        """
    )
