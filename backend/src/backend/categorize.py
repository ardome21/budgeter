"""Choosing a category for an incoming transaction.

One function, used by every path that writes a row, for the same reason
`merchants.resolve` is one function: when this was written out by hand at each
call site it came out correct in three places and missing in two.

The bank decides, unless you have said otherwise for that merchant. A
`MerchantRule` comes first ("Netflix is a subscription"), then Plaid's label
through the map — detailed first, then primary — and only when nothing
answers does a row land in `Uncategorised`. There used to be a rung that
learned from edits: "where you last filed this merchant". It is gone, and the
rule is not it: a rule is set on purpose, from the list of regular charges,
while an edit to one row stays an edit to one row — revisions and re-syncs
leave `category_id` alone, but it teaches nothing to the next.

Three rules are not conveniences. `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` resolves
to a TRANSFER category: a card payment is last month's spending moving between
two accounts you own, and counting it made August's largest "purchase" a
$207.45 row that described nothing. And Bilt's same-day "Payment - Bilt
Housing" credit, which Plaid labels `INCOME_RENTAL`, is a Transfer too — it is
the other half of the rent charge, and as income it would count every month's
rent as earned as well as spent. And the checking-account debit that funds it,
`BILT CARD HOUSING`, is a Transfer — Plaid labels it rent, so without this
every month's rent was counted twice: once leaving Chase, once on the card.
That one cannot go in the map, because the map only sees Plaid's labels and
the rent charge on the card carries the very same label; it is told apart by
its descriptor, in `TRANSFERS_BY_DESCRIPTION`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, CategoryKind, MerchantRule, PlaidCategoryMap

UNCATEGORISED = "Uncategorised"

# Descriptors that are money moving between your own accounts whatever Plaid
# labels them. Matched as a case-insensitive prefix. Kept short on purpose:
# anything Plaid labels correctly belongs in the map instead.
TRANSFERS_BY_DESCRIPTION = ("BILT CARD HOUSING",)
TRANSFER = "Transfer"


def from_plaid(
    db: Session, detailed: str | None = None, primary: str | None = None
) -> Category | None:
    """The mapped category for Plaid's own labels — detailed, then primary."""
    for value in (detailed, primary):
        if not value:
            continue
        category = db.scalar(
            select(Category)
            .join(PlaidCategoryMap, PlaidCategoryMap.category_id == Category.id)
            .where(PlaidCategoryMap.plaid_category == value)
        )
        if category is not None:
            return category
    return None


def fallback(db: Session) -> Category:
    """Somewhere to put a charge nothing suggests a home for.

    Created on demand rather than seeded, so it only exists once something has
    actually needed it.
    """
    category = db.scalar(select(Category).where(Category.name == UNCATEGORISED))
    if category is None:
        category = Category(
            name=UNCATEGORISED, kind=CategoryKind.SPENDING, sort_order=999
        )
        db.add(category)
        db.flush()
    return category


def by_description(db: Session, description: str | None) -> Category | None:
    """A Transfer for a descriptor in `TRANSFERS_BY_DESCRIPTION`, else None."""
    text = (description or "").upper()
    if not any(text.startswith(prefix) for prefix in TRANSFERS_BY_DESCRIPTION):
        return None
    return db.scalar(select(Category).where(Category.name == TRANSFER))


def resolve_category(
    db: Session,
    detailed: str | None = None,
    primary: str | None = None,
    description: str | None = None,
    merchant: str | None = None,
) -> Category:
    """The category to file a row under. Never None — see `fallback`.

    The descriptor rules come before your merchant rules on purpose: they
    exist so a transfer never counts as spending, and no rule should be able
    to undo that by accident.
    """
    return (
        by_description(db, description)
        or by_rule(db, merchant or description)
        or from_plaid(db, detailed, primary)
        or fallback(db)
    )


def by_rule(db: Session, merchant: str | None) -> Category | None:
    if not merchant:
        return None
    return db.scalar(
        select(Category)
        .join(MerchantRule, MerchantRule.category_id == Category.id)
        .where(MerchantRule.merchant == merchant)
    )


def bank_guess(db: Session, txn) -> Category:
    """Where a row would go with no merchant rule: what the bank's labels say."""
    return (
        by_description(db, txn.description)
        or from_plaid(db, txn.plaid_category_detailed, txn.plaid_category_primary)
        or fallback(db)
    )
