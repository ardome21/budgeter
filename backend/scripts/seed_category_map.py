"""What each of Plaid's categories means in your categories.

Plaid's personal finance taxonomy has a `primary` (FOOD_AND_DRINK) and a
`detailed` (FOOD_AND_DRINK_GROCERIES). Both are mapped here, detailed first,
so one rule can say "groceries are Groceries" while everything else under
FOOD_AND_DRINK falls through to Food and Drinks — without a row per detailed
value, of which there are over a hundred.

The one row that is not a convenience is LOAN_PAYMENTS_CREDIT_CARD_PAYMENT.
A card payment is last month's spending moving between two accounts you own;
counting it as spending made August 2026's second-largest "purchase" a $207.45
row that described nothing. Filing it as a TRANSFER category is what keeps it
out of every figure that claims you spent something.

Idempotent, like seed_categories.py: an existing mapping is left exactly as it
is, so running this twice cannot undo a remapping you made in Settings.

    uv run python scripts/seed_category_map.py
"""

from __future__ import annotations

from sqlalchemy import func, select

from backend.db import SessionLocal
from backend.models import Category, PlaidCategoryMap

# Plaid value -> the category name it means here. Detailed values are listed
# only where they disagree with their primary.
MAPPING: dict[str, str] = {
    # --- the one that matters -----------------------------------------------
    # Not spending. See the module docstring.
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "Transfer",
    # --- money in and money moved -------------------------------------------
    "INCOME": "Income",
    # Bilt's same-day credit against the rent charge. Plaid calls it rental
    # income; it is the other half of paying rent, and as income it would
    # count every month's rent as earned as well as spent.
    "INCOME_RENTAL": "Transfer",
    "TRANSFER_IN": "Transfer",
    "TRANSFER_OUT": "Transfer",
    # Money you still have, worth seeing apart from money that merely moved.
    "TRANSFER_IN_SAVINGS": "Savings",
    "TRANSFER_OUT_SAVINGS": "Savings",
    # Plaid files investment contributions as a transfer; they are savings.
    "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS": "Savings",
    "TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS": "Savings",
    # --- the roof over your head --------------------------------------------
    "RENT_AND_UTILITIES": "Utilities",
    "RENT_AND_UTILITIES_RENT": "Rent",
    # --- everything else ----------------------------------------------------
    "FOOD_AND_DRINK": "Food and Drinks",
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "TRANSPORTATION": "Transportation",
    "TRAVEL": "Travel",
    "ENTERTAINMENT": "Entertainment",
    "PERSONAL_CARE": "Self Care",
    "MEDICAL": "Self Care",
    "GENERAL_MERCHANDISE": "Shopping",
    "HOME_IMPROVEMENT": "Shopping",
    "LOAN_PAYMENTS": "Loan Payments",
    "GOVERNMENT_AND_NON_PROFIT": "Misc",
    "GOVERNMENT_AND_NON_PROFIT_DONATIONS": "Donations",
    "GENERAL_SERVICES": "Misc",
    "GENERAL_SERVICES_INSURANCE": "Insurance",
    "BANK_FEES": "Misc",
    "OTHER": "Misc",
}


def main() -> int:
    with SessionLocal() as session:
        categories = {
            name.lower(): id_
            for id_, name in session.execute(select(Category.id, Category.name)).all()
        }

        added = 0
        missing: list[str] = []
        for plaid_value, category_name in MAPPING.items():
            category_id = categories.get(category_name.lower())
            if category_id is None:
                # A category this map names but the database does not have.
                # Said out loud rather than created: seed_categories.py owns
                # what categories exist, and inventing one here would put two
                # scripts in charge of the same table.
                missing.append(category_name)
                continue

            existing = session.scalar(
                select(PlaidCategoryMap).where(
                    func.upper(PlaidCategoryMap.plaid_category) == plaid_value
                )
            )
            if existing is not None:
                continue
            session.add(
                PlaidCategoryMap(
                    plaid_category=plaid_value, category_id=category_id
                )
            )
            added += 1
        session.commit()

    unchanged = len(MAPPING) - added - len(missing)
    print(f"{added} mappings added, {unchanged} already present.")
    if missing:
        unique = sorted(set(missing))
        print(
            f"no such category: {', '.join(unique)} — "
            "run scripts/seed_categories.py first, or edit MAPPING"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
