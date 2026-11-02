"""The endpoints, through the client."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import categorize
from backend.models import (
    Balance,
    Category,
    CategoryKind,
    PlaidCategoryMap,
    Transaction,
)


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
    plan = client.get("/api/budgets/2026/3/plan").json()
    assert plan["budget_from"] == "2026-03-01"
    assert plan["rows"][0]["budget"] == "400.00"

    client.put("/api/budgets/2026/3", json=[])

    plan = client.get("/api/budgets/2026/3/plan").json()
    assert plan["budget_from"] is None
    assert plan["rows"][0]["budget"] is None


def test_a_budget_holds_until_it_is_changed(
    client: TestClient, session: Session, spending_category
):
    """Budgets went unmaintained for a year in the version this replaced, and
    the reason was that every month started empty. Set once, a budget carries
    into every later month until one of them is given its own."""
    client.put(
        "/api/budgets/2026/3",
        json=[{"category_id": spending_category.id, "amount": "400.00"}],
    )
    client.put(
        "/api/budgets/2026/6",
        json=[{"category_id": spending_category.id, "amount": "250.00"}],
    )

    def budget(month: int) -> tuple[str | None, str]:
        body = client.get(f"/api/month/2026/{month}").json()
        return body["budget_from"], body["budgeted"]

    assert budget(2) == (None, "0.00")
    assert budget(3) == ("2026-03-01", "400.00")
    assert budget(5) == ("2026-03-01", "400.00")
    assert budget(6) == ("2026-06-01", "250.00")
    assert budget(9) == ("2026-06-01", "250.00")


def test_the_plan_proposes_from_last_month_rounded_up(
    client: TestClient, session: Session, add_transaction, account, spending_category
):
    """$842.17 proposes $850: a target, not a receipt — and rounded up, because
    rounding down sets a budget already broken last month. A category with
    nothing spent proposes nothing rather than a $0 target."""
    idle = Category(name="Idle Test", kind=CategoryKind.SPENDING, sort_order=60)
    session.add(idle)
    session.flush()
    add_transaction(
        account=account, category=spending_category,
        amount="842.17", occurred_on=date(2026, 10, 9),
    )
    add_transaction(
        account=account, category=spending_category,
        amount="100.00", occurred_on=date(2026, 9, 9),
    )

    plan = client.get("/api/budgets/2026/11/plan").json()

    assert plan["last_month"] == "2026-10-01"
    assert plan["last_month_spent"] == "842.17"
    rows = {r["category_name"]: r for r in plan["rows"]}
    shopping = rows[spending_category.name]
    assert shopping["last_month"] == "842.17"
    assert shopping["from_last_month"] == "850.00"
    # Median of Aug (nothing), Sep 100 and Oct 842.17.
    assert shopping["typical"] == "100.00"
    assert shopping["from_typical"] == "100.00"
    assert shopping["months_seen"] == 2
    assert rows["Idle Test"]["from_last_month"] is None
    assert rows["Idle Test"]["last_month"] == "0.00"
    assert plan["rows"][0]["category_name"] == spending_category.name


def test_the_plan_proposes_income_rounded_down(
    client: TestClient, add_transaction, account, income_category
):
    """Income is planned the other way up: shown positive, and rounded down,
    because a plan you fall short of spends money that never arrived."""
    add_transaction(
        account=account, category=income_category,
        amount="-4957.23", occurred_on=date(2026, 10, 15),
    )

    plan = client.get("/api/budgets/2026/11/plan").json()

    [income] = plan["income"]
    assert income["last_month"] == "4957.23"
    assert income["from_last_month"] == "4950.00"
    assert plan["last_month_income"] == "4957.23"
    assert all(r["category_id"] != income_category.id for r in plan["rows"])


def test_the_month_says_what_is_left(
    client: TestClient, add_transaction, account, spending_category, income_category
):
    """Half the pay has landed and $1,000 is spent. As it stands, $1,500 is
    left; once the rest of the expected $5,000 arrives, $4,000 is; and the plan
    itself ($3,000 of spending) leaves $2,000."""
    client.put(
        "/api/budgets/2026/3",
        json=[
            {"category_id": spending_category.id, "amount": "3000.00"},
            {"category_id": income_category.id, "amount": "5000.00"},
        ],
    )
    add_transaction(
        account=account, category=income_category,
        amount="-2500.00", occurred_on=date(2026, 3, 15),
    )
    add_transaction(
        account=account, category=spending_category,
        amount="1000.00", occurred_on=date(2026, 3, 16),
    )

    body = client.get("/api/month/2026/3").json()

    assert body["income_expected"] == "5000.00"
    assert body["left"] == "1500.00"
    assert body["left_expected"] == "4000.00"
    assert body["left_planned"] == "2000.00"
    # Spending budgets only, still: the income target is not "budgeted".
    assert body["budgeted"] == "3000.00"


def test_no_income_budget_makes_no_claim_about_what_is_left(
    client: TestClient, add_transaction, account, spending_category
):
    add_transaction(
        account=account, category=spending_category,
        amount="10.00", occurred_on=date(2026, 3, 16),
    )

    body = client.get("/api/month/2026/3").json()

    assert body["left"] == "-10.00"
    assert body["left_expected"] is None
    assert body["left_planned"] is None


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


def test_filing_one_row_files_the_rest_of_that_merchant(
    client: TestClient, session, account, spending_category, add_transaction
):
    """Uber Eats appears ten times. Filing it should be one decision, not ten."""
    unfiled = _uncategorised(session)
    rows = [
        add_transaction(
            account=account, category=unfiled, amount="20.00",
            occurred_on=date(2026, 3, day), merchant="Uber Eats",
        )
        for day in (1, 2, 3)
    ]

    body = client.patch(
        f"/api/transactions/{rows[0].id}",
        json={"category_id": spending_category.id, "apply_to_merchant": True},
    ).json()

    assert body["also_filed"] == 2
    session.expire_all()
    assert {r.category_id for r in rows} == {spending_category.id}


def test_applying_to_a_merchant_never_overwrites_a_hand_filing(
    client: TestClient, session, account, spending_category, income_category,
    add_transaction,
):
    """A single Amazon charge under Travel may well be deliberate. A bulk
    action that overwrites it destroys a decision nobody asked it to touch —
    and is what makes this safe to press twice."""
    unfiled = _uncategorised(session)
    deliberate = add_transaction(
        account=account, category=income_category, amount="20.00",
        occurred_on=date(2026, 3, 1), merchant="Amazon",
    )
    still_unfiled = add_transaction(
        account=account, category=unfiled, amount="20.00",
        occurred_on=date(2026, 3, 2), merchant="Amazon",
    )

    body = client.patch(
        f"/api/transactions/{still_unfiled.id}",
        json={"category_id": spending_category.id, "apply_to_merchant": True},
    ).json()

    assert body["also_filed"] == 0
    session.expire_all()
    assert deliberate.category_id == income_category.id


def test_refiling_places_what_the_bank_labelled(
    client: TestClient, session, account, spending_category, add_transaction
):
    """The bulk version, for history that arrived before anything could file
    it. Rows nothing can place are reported, not guessed at."""
    unfiled = _uncategorised(session)
    session.add(
        PlaidCategoryMap(
            plaid_category="FOOD_AND_DRINK", category_id=spending_category.id
        )
    )
    placeable = add_transaction(
        account=account, category=unfiled, amount="20.00",
        occurred_on=date(2026, 3, 1), merchant="Some Cafe",
    )
    placeable.plaid_category_primary = "FOOD_AND_DRINK"
    add_transaction(
        account=account, category=unfiled, amount="5.00",
        occurred_on=date(2026, 3, 2), merchant="Mystery",
    )
    session.flush()

    body = client.post("/api/transactions/refile-uncategorised").json()

    assert body == {"moved": 1, "left": 1}
    session.expire_all()
    assert placeable.category_id == spending_category.id


def _uncategorised(session):
    existing = session.scalar(
        select(Category).where(Category.name == "Uncategorised")
    )
    if existing is not None:
        return existing
    created = Category(
        name="Uncategorised", kind=CategoryKind.SPENDING, sort_order=999
    )
    session.add(created)
    session.flush()
    return created


def test_categories_say_how_many_rows_they_hold(
    client: TestClient, add_transaction, account, spending_category
):
    """So the screen can offer delete only where it would work."""
    add_transaction(account=account, category=spending_category,
                    amount="5.00", occurred_on=date(2025, 3, 4))

    rows = {c["id"]: c for c in client.get("/api/categories").json()}

    assert rows[spending_category.id]["transaction_count"] >= 1


def test_a_category_the_plaid_map_points_at_can_still_be_deleted(
    client: TestClient, session: Session
):
    """Loan Payments had no rows and could not be deleted: the map's foreign
    key refused it with a bare 500."""
    loans = Category(name="Old Loans", kind=CategoryKind.SPENDING)
    session.add(loans)
    session.flush()
    session.add(PlaidCategoryMap(plaid_category="TEST_LOANS", category_id=loans.id))
    session.flush()

    assert client.delete(f"/api/categories/{loans.id}").status_code == 204
    assert session.scalar(
        select(PlaidCategoryMap).where(PlaidCategoryMap.plaid_category == "TEST_LOANS")
    ) is None


def _three_months(add, account, category, merchant="Netflix", amount="21.64"):
    for m in (6, 7, 8):
        add(account=account, category=category, amount=amount,
            occurred_on=date(2025, m, 2), merchant=merchant)


def test_regular_charges_are_listed_with_where_they_are_filed(
    client: TestClient, add_transaction, account, spending_category
):
    _three_months(add_transaction, account, spending_category)

    rows = client.get("/api/regular", params={"month": "2025-09-01"}).json()

    assert [(r["merchant"], r["amount"], r["day"], r["ruled"]) for r in rows] == [
        ("Netflix", "21.64", 2, False)
    ]


def test_a_merchant_rule_refiles_its_history_but_not_a_hand_filed_row(
    client: TestClient, session: Session, add_transaction, account,
    spending_category,
):
    """Netflix is a subscription, from now on and back through its history.
    A charge you filed elsewhere by hand stays where you put it."""
    subs = Category(name="Subscriptions Test", kind=CategoryKind.SPENDING)
    elsewhere = Category(name="Gifts Test", kind=CategoryKind.SPENDING)
    session.add_all([subs, elsewhere])
    session.flush()
    _three_months(add_transaction, account, spending_category)
    gift = add_transaction(account=account, category=elsewhere, amount="21.64",
                           occurred_on=date(2025, 8, 20), merchant="Netflix")
    # The other rows sit where the bank's label put them: no label, so the
    # fallback. Filed under Groceries by the fixture instead, so say so.
    netflix = select(Transaction).where(Transaction.merchant == "Netflix")
    for txn in session.scalars(netflix):
        if txn.id != gift.id:
            txn.category_id = categorize.fallback(session).id
    session.flush()

    body = client.put("/api/merchant-rules",
                      json={"merchant": "Netflix", "category_id": subs.id}).json()

    assert body["moved"] == 3
    session.expire_all()
    filed = {t.id: t.category_id for t in session.scalars(
        select(Transaction).where(Transaction.merchant == "Netflix"))}
    assert filed.pop(gift.id) == elsewhere.id
    assert set(filed.values()) == {subs.id}
    # And the next one follows the rule.
    assert categorize.resolve_category(session, merchant="Netflix").id == subs.id

    cleared = client.put("/api/merchant-rules",
                         json={"merchant": "Netflix", "category_id": None}).json()
    assert cleared["moved"] == 3
    after = categorize.resolve_category(session, merchant="Netflix")
    assert after.name == "Uncategorised"
