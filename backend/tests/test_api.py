"""The endpoints, through the client."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.models import Balance, Category, CategoryKind


def test_money_crosses_the_wire_as_a_string(
    client: TestClient, session: Session, add_transaction, account, spending_category
):
    """JavaScript has no decimal type. A JSON number becomes a float the moment
    it is parsed, and a budget three cents off with no explanation is how that
    shows up."""
    add_transaction(
        account=account, category=spending_category,
        amount="1234.56", occurred_on=date(2026, 3, 4),
    )

    body = client.get("/api/month/2026/3").json()

    assert body["spent"] == "1234.56"
    assert isinstance(body["spent"], str)


def test_months_lists_only_months_with_rows(
    client: TestClient, add_transaction, account, spending_category
):
    add_transaction(
        account=account, category=spending_category,
        amount="5.00", occurred_on=date(2026, 3, 4),
    )
    add_transaction(
        account=account, category=spending_category,
        amount="5.00", occurred_on=date(2026, 1, 9),
    )

    assert client.get("/api/months").json() == ["2026-03-01", "2026-01-01"]


def test_accounts_carry_their_own_latest_balance(
    client: TestClient, session: Session, account
):
    """Accounts are read on different days — a brokerage posts overnight, a
    card posts immediately. Taking the newest date across all accounts would
    blank every account that did not report today."""
    other = type(account)(
        plaid_account_id="second", name="Savings", institution="Test Bank",
        account_type="depository", subtype="savings", is_liability=False,
    )
    session.add(other)
    session.flush()
    session.add_all(
        [
            Balance(account_id=account.id, as_of=date(2026, 3, 1),
                    amount=Decimal("10.00")),
            Balance(account_id=other.id, as_of=date(2026, 2, 1),
                    amount=Decimal("99.00")),
        ]
    )
    session.flush()

    rows = {row["name"]: row for row in client.get("/api/accounts").json()}

    assert rows["Test Checking"]["balance"] == "10.00"
    assert rows["Savings"]["balance"] == "99.00"
    assert rows["Savings"]["balance_as_of"] == "2026-02-01"


def test_a_category_in_use_cannot_be_deleted(
    client: TestClient, add_transaction, account, spending_category
):
    """Deleting it would orphan the rows or silently re-file them. Both lose
    information the person clicking delete did not mean to lose."""
    add_transaction(
        account=account, category=spending_category,
        amount="5.00", occurred_on=date(2026, 3, 4),
    )

    response = client.delete(f"/api/categories/{spending_category.id}")

    assert response.status_code == 409
    assert "1 transactions" in response.json()["detail"]


def test_budgets_are_replaced_wholesale(
    client: TestClient, session: Session, spending_category
):
    """The screen edits a month as one table and saves once, so the endpoint
    takes the month as one document. Leaving a category out removes its target;
    an amount of zero is a deliberate nothing."""
    client.put(
        "/api/budgets/2026/3",
        json=[{"category_id": spending_category.id, "amount": "400.00"}],
    )
    assert client.get("/api/budgets/2026/3").json()[0]["amount"] == "400.00"

    client.put("/api/budgets/2026/3", json=[])

    assert client.get("/api/budgets/2026/3").json() == []


def test_budgets_can_be_carried_forward(
    client: TestClient, session: Session, spending_category
):
    """Budgets went unmaintained for a year in the version this replaced, and
    the reason was that every month started empty."""
    client.put(
        "/api/budgets/2026/3",
        json=[{"category_id": spending_category.id, "amount": "400.00"}],
    )

    copied = client.post("/api/budgets/2026/4/copy").json()

    assert copied[0]["amount"] == "400.00"
    assert copied[0]["category_id"] == spending_category.id


def test_a_transaction_can_be_refiled_but_not_rewritten(
    client: TestClient, session: Session, add_transaction, account, spending_category
):
    """Correcting a guess is a different act from rewriting a fact. The
    category and the merchant are answers to the row; the amount and the date
    are the bank's account of what happened."""
    other = Category(name="Travel Test", kind=CategoryKind.SPENDING, sort_order=50)
    session.add(other)
    session.flush()
    txn = add_transaction(
        account=account, category=spending_category,
        amount="52.10", occurred_on=date(2026, 3, 4),
    )

    body = client.patch(
        f"/api/transactions/{txn.id}",
        json={"category_id": other.id, "merchant": "Harris Teeter", "amount": "1.00"},
    ).json()

    assert body["category_name"] == "Travel Test"
    assert body["merchant"] == "Harris Teeter"
    # The amount in the payload is ignored — there is no field for it.
    assert body["amount"] == "52.10"


def test_merchants_are_ranked_by_use(
    client: TestClient, add_transaction, account, spending_category
):
    """The whole of what used to be three tables, two screens and a merge
    queue: the names already in use, commonest first, offered at entry."""
    for merchant in ("Harris Teeter", "Harris Teeter", "Lyft"):
        add_transaction(
            account=account, category=spending_category, amount="5.00",
            occurred_on=date(2026, 3, 4), merchant=merchant,
        )

    assert client.get("/api/merchants").json() == ["Harris Teeter", "Lyft"]
