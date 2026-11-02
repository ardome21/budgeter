"""Choosing a category, and the order the choice is made in.

Plaid's label through the map — detailed, then primary — and Uncategorised
only when neither is mapped.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.categorize import resolve_category
from backend.models import Category, CategoryKind, PlaidCategoryMap, Transaction


@pytest.fixture
def groceries(session: Session) -> Category:
    return _category(session, "Groceries", CategoryKind.SPENDING)


@pytest.fixture
def card_payments(session: Session) -> Category:
    return _category(session, "Transfer", CategoryKind.TRANSFER)


def _category(session: Session, name: str, kind: CategoryKind) -> Category:
    existing = session.scalar(select(Category).where(Category.name == name))
    if existing is not None:
        return existing
    created = Category(name=name, kind=kind, sort_order=0)
    session.add(created)
    session.flush()
    return created


def _map(session: Session, plaid_value: str, category: Category) -> None:
    session.add(
        PlaidCategoryMap(plaid_category=plaid_value, category_id=category.id)
    )
    session.flush()


def test_plaids_label_files_a_row_nothing_else_can_place(
    session: Session, groceries
):
    """The whole reason the map exists. Without it every one of the first 71
    transactions sat in Uncategorised."""
    _map(session, "FOOD_AND_DRINK_GROCERIES", groceries)

    chosen = resolve_category(
        session, "FOOD_AND_DRINK_GROCERIES", "FOOD_AND_DRINK"
    )

    assert chosen.id == groceries.id


def test_the_detailed_label_wins_over_the_primary(session: Session, groceries):
    """It is the only thing separating rent from the power bill, and groceries
    from a restaurant — both pairs are two different budgets."""
    eating_out = _category(session, "Food and Drinks", CategoryKind.SPENDING)
    _map(session, "FOOD_AND_DRINK", eating_out)
    _map(session, "FOOD_AND_DRINK_GROCERIES", groceries)

    chosen = resolve_category(
        session, "FOOD_AND_DRINK_GROCERIES", "FOOD_AND_DRINK"
    )

    assert chosen.id == groceries.id


def test_the_primary_catches_a_detailed_value_nobody_mapped(
    session: Session, groceries
):
    """There are over a hundred detailed values. The primary is what stops the
    map needing a row for each one."""
    eating_out = _category(session, "Food and Drinks", CategoryKind.SPENDING)
    _map(session, "FOOD_AND_DRINK", eating_out)

    chosen = resolve_category(
        session, "FOOD_AND_DRINK_COFFEE", "FOOD_AND_DRINK"
    )

    assert chosen.id == eating_out.id


def test_nothing_known_lands_in_uncategorised(session: Session):
    chosen = resolve_category(session, None, None)

    assert chosen.name == "Uncategorised"


def test_a_card_payment_is_not_spending(
    client: TestClient, session: Session, account, card_payments
):
    """The $207.45 regression, now caught on the way in.

    A credit card payment is last month's spending moving between two accounts
    you own. Counting it made August 2026's second largest 'purchase' a
    transfer that described nothing, ranked above the power bill. Plaid labels
    these reliably as LOAN_PAYMENTS_CREDIT_CARD_PAYMENT, so the map files them
    as a TRANSFER and every spending total drops them by kind.
    """
    _map(session, "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", card_payments)

    chosen = resolve_category(
        session,
        "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
        "LOAN_PAYMENTS",
    )
    assert chosen.kind == CategoryKind.TRANSFER

    session.add(
        Transaction(
            account_id=account.id,
            category_id=chosen.id,
            occurred_on=date(2026, 8, 15),
            amount=Decimal("207.45"),
            description="INTERNET PAYMENT THANK YOU",
            merchant="Internet Payment Thank You",
            created_at=datetime.now(UTC),
        )
    )
    session.flush()

    body = client.get("/api/month/2026/8").json()

    assert body["spent"] == "0.00"
    assert body["transferred"] == "207.45"


def test_bilts_rent_credit_is_not_income(session: Session, card_payments):
    """Bilt posts the rent as a charge and, the same day, a matching credit
    Plaid labels INCOME_RENTAL. As income, every month's rent would be counted
    as earned as well as spent."""
    income = _category(session, "Income", CategoryKind.INCOME)
    _map(session, "INCOME", income)
    _map(session, "INCOME_RENTAL", card_payments)

    chosen = resolve_category(session, "INCOME_RENTAL", "INCOME")

    assert chosen.kind == CategoryKind.TRANSFER


def test_the_checking_debit_that_funds_bilt_rent_is_a_transfer(
    session: Session, card_payments
):
    """Plaid labels both the Chase debit and the Bilt card's rent charge as
    rent. Counting both doubled every month's rent; the card charge is the
    rent, the debit is money moving to the card."""
    rent = _category(session, "Rent", CategoryKind.SPENDING)
    _map(session, "RENT_AND_UTILITIES_RENT", rent)

    debit = resolve_category(
        session, "RENT_AND_UTILITIES_RENT", "RENT_AND_UTILITIES",
        "BILT CARD HOUSING PPD ID: 1844372402",
    )
    charge = resolve_category(
        session, "RENT_AND_UTILITIES_RENT", "RENT_AND_UTILITIES",
        "Bilt Housing Payment",
    )

    assert debit.id == card_payments.id
    assert charge.id == rent.id
