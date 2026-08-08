"""The linked-account sync, with Plaid itself replaced.

Nothing here talks to Plaid. `plaid_client.sync_transactions` is the whole of
the network surface the sync endpoint touches, so faking that one function
leaves everything worth testing — cursor discipline, revisions, the pending
filter, identity by transaction id — running against real Postgres.

The cursor rules are what these mostly exist for. Getting them wrong loses real
transactions silently, which is the one failure this feature must not have.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from backend import plaid_client
from backend.models import (
    Account,
    Category,
    CategoryKind,
    PlaidAccount,
    PlaidItem,
    Transaction,
    TransactionSource,
)
from backend.plaid_client import LinkedTransaction, SyncDiff
from backend.routers import plaid_link


@pytest.fixture
def linked(session, monkeypatch):
    """One linked institution with one account, and Plaid stubbed out."""
    # `plaid_configured` is a read-only property over these three, so the
    # fields get set rather than the answer.
    monkeypatch.setattr(plaid_link.settings, "plaid_client_id", "test-client")
    monkeypatch.setattr(plaid_link.settings, "plaid_secret", SecretStr("test-secret"))
    monkeypatch.setattr(plaid_link.settings, "plaid_token_key", SecretStr("test-key"))
    monkeypatch.setattr(plaid_link, "decrypt_token", lambda token: "access-test")

    account = Account(institution="Chase", name="Sapphire", is_retirement=False)
    session.add(account)
    session.flush()

    item = PlaidItem(
        item_id="item-test",
        institution_id="ins_1",
        institution_name="Chase",
        access_token="ciphertext",
        cursor=None,
        pending_cursor=None,
        sync_start_on=date(2026, 1, 1),
        needs_reauth=False,
        linked_at=datetime.now(UTC),
    )
    session.add(item)
    session.flush()
    session.add(
        PlaidAccount(
            item_id=item.id,
            plaid_account_id="acct-1",
            account_id=account.id,
            mask="4242",
            account_type="depository",
            subtype="checking",
        )
    )
    session.flush()
    return item, account


def txn(
    transaction_id: str,
    *,
    amount: str = "12.34",
    on: date = date(2026, 6, 1),
    name: str = "HARRIS TEETER 0412",
    pending: bool = False,
    merchant: str | None = None,
) -> LinkedTransaction:
    return LinkedTransaction(
        transaction_id=transaction_id,
        plaid_account_id="acct-1",
        occurred_on=on,
        name=name,
        merchant_name=merchant,
        amount=Decimal(amount),
        pending=pending,
        category="General Merchandise",
    )


def feed(monkeypatch, diff: SyncDiff) -> None:
    monkeypatch.setattr(plaid_link, "sync_transactions", lambda token, cursor: diff)


def test_sync_offers_added_rows_without_writing_them(client, session, linked, monkeypatch):
    _item, _ = linked
    feed(monkeypatch, SyncDiff(added=[txn("t1")], modified=[], removed=[], cursor="c1"))

    body = client.post("/api/plaid/sync").json()

    assert [r["key"] for r in body["rows"]] == ["t1"]
    assert session.scalar(
        select(Transaction).where(Transaction.source_ref == "t1")
    ) is None, "sync must write nothing — the preview is the whole point"


def test_cursor_moves_only_on_commit(client, session, linked, monkeypatch, category_id):
    """Abandoning a preview re-offers the same rows rather than skipping them."""
    item, account = linked
    feed(monkeypatch, SyncDiff(added=[txn("t1")], modified=[], removed=[], cursor="c1"))

    client.post("/api/plaid/sync")
    session.refresh(item)
    assert item.cursor is None, "a preview that was never committed must not advance"
    assert item.pending_cursor == "c1"

    # Same rows come back, because the cursor never moved.
    assert len(client.post("/api/plaid/sync").json()["rows"]) == 1

    client.post(
        "/api/plaid/commit",
        json={
            "rows": [
                {
                    "key": "t1",
                    "account_id": account.id,
                    "occurred_on": "2026-06-01",
                    "raw_description": "HARRIS TEETER 0412",
                    "amount": "12.34",
                    "category_id": category_id,
                }
            ]
        },
    )
    session.refresh(item)
    assert item.cursor == "c1"
    assert item.pending_cursor is None


def test_committed_rows_are_not_offered_twice(
    client, session, linked, monkeypatch, category_id
):
    """A rewind replays the batch; already-committed rows must not reappear."""
    item, account = linked
    feed(monkeypatch, SyncDiff(added=[txn("t1")], modified=[], removed=[], cursor="c1"))
    client.post("/api/plaid/sync")
    client.post(
        "/api/plaid/commit",
        json={
            "rows": [
                {
                    "key": "t1",
                    "account_id": account.id,
                    "occurred_on": "2026-06-01",
                    "raw_description": "HARRIS TEETER 0412",
                    "amount": "12.34",
                    "category_id": category_id,
                }
            ]
        },
    )

    client.post(f"/api/plaid/items/{item.id}/rewind")
    body = client.post("/api/plaid/sync").json()

    assert body["rows"] == []
    session.refresh(item)
    assert item.cursor == "c1", (
        "a replay of nothing but known rows must still advance, "
        "or every refresh repeats it forever"
    )


def test_pending_charges_are_not_offered(client, linked, monkeypatch):
    """A pending charge is withdrawn and re-sent when it posts, usually at a
    different amount. Offering it means reviewing the same coffee twice."""
    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("t1", pending=True), txn("t2")],
            modified=[],
            removed=[],
            cursor="c1",
        ),
    )
    body = client.post("/api/plaid/sync").json()
    assert [r["key"] for r in body["rows"]] == ["t2"]


def test_rows_before_the_start_date_are_not_offered(client, linked, monkeypatch):
    """The floor that stops a first sync re-offering the imported workbook."""
    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("old", on=date(2025, 5, 5)), txn("new")],
            modified=[],
            removed=[],
            cursor="c1",
        ),
    )
    body = client.post("/api/plaid/sync").json()
    assert [r["key"] for r in body["rows"]] == ["new"]


def test_a_revised_charge_is_updated_in_place(
    client, session, linked, monkeypatch, category_id
):
    """The case a content hash gets wrong: same charge, new amount and date."""
    _item, account = linked
    feed(monkeypatch, SyncDiff(added=[txn("t1")], modified=[], removed=[], cursor="c1"))
    client.post("/api/plaid/sync")
    client.post(
        "/api/plaid/commit",
        json={
            "rows": [
                {
                    "key": "t1",
                    "account_id": account.id,
                    "occurred_on": "2026-06-01",
                    "raw_description": "HARRIS TEETER 0412",
                    "amount": "12.34",
                    "category_id": category_id,
                }
            ]
        },
    )

    feed(
        monkeypatch,
        SyncDiff(
            added=[],
            modified=[txn("t1", amount="15.00", on=date(2026, 6, 3))],
            removed=[],
            cursor="c2",
        ),
    )
    body = client.post("/api/plaid/sync").json()

    assert body["updated"] == 1
    assert body["rows"] == [], "a revision is the bank's correction, not a question"
    row = session.scalar(select(Transaction).where(Transaction.source_ref == "t1"))
    assert row.amount == Decimal("15.00")
    assert row.occurred_on == date(2026, 6, 3)


def test_a_withdrawn_charge_is_deleted(
    client, session, linked, monkeypatch, category_id
):
    _item, account = linked
    feed(monkeypatch, SyncDiff(added=[txn("t1")], modified=[], removed=[], cursor="c1"))
    client.post("/api/plaid/sync")
    client.post(
        "/api/plaid/commit",
        json={
            "rows": [
                {
                    "key": "t1",
                    "account_id": account.id,
                    "occurred_on": "2026-06-01",
                    "raw_description": "HARRIS TEETER 0412",
                    "amount": "12.34",
                    "category_id": category_id,
                }
            ]
        },
    )

    feed(monkeypatch, SyncDiff(added=[], modified=[], removed=["t1"], cursor="c2"))
    body = client.post("/api/plaid/sync").json()

    assert body["removed"] == 1
    assert (
        session.scalar(select(Transaction).where(Transaction.source_ref == "t1"))
        is None
    )


def test_a_revision_never_touches_a_row_from_another_source(
    client, session, linked, monkeypatch, category_id
):
    """source_ref is a cell reference on workbook rows. A LINKED transaction id
    that happened to collide with one must not rewrite it."""
    from backend.routers.transactions import get_or_create_period

    period = get_or_create_period(session, 2026, 6)
    session.add(
        Transaction(
            occurred_on=date(2026, 6, 1),
            period_id=period.id,
            raw_description="Groceries",
            category_id=category_id,
            amount=Decimal("99.99"),
            is_recurring=False,
            source=TransactionSource.WORKBOOK,
            source_ref="t1",
        )
    )
    session.flush()

    feed(
        monkeypatch,
        SyncDiff(added=[], modified=[txn("t1", amount="1.00")], removed=["t1"], cursor="c1"),
    )
    body = client.post("/api/plaid/sync").json()

    assert body["updated"] == 0
    assert body["removed"] == 0
    survivor = session.scalar(
        select(Transaction).where(
            Transaction.source_ref == "t1",
            Transaction.source == TransactionSource.WORKBOOK,
        )
    )
    assert survivor.amount == Decimal("99.99")


def test_plaid_merchant_name_snaps_to_the_spelling_already_in_use(
    client, session, linked, monkeypatch, category_id
):
    """Plaid's name is cleaner than the descriptor, but it still must join the
    history already on file instead of starting a second merchant beside it.

    The merchant is deliberately one that cannot exist: these tests run against
    the real development database, and a realistic name like `Netflix` already
    carries three years of its own history that outvotes anything set up here.
    """
    from backend.routers.transactions import get_or_create_period

    period = get_or_create_period(session, 2026, 5)
    session.add(
        Transaction(
            occurred_on=date(2026, 5, 1),
            period_id=period.id,
            raw_description="Zzyzx Roasters",
            merchant_key="Zzyzx Roasters",
            category_id=category_id,
            amount=Decimal("12.99"),
            is_recurring=True,
            source=TransactionSource.WORKBOOK,
            source_ref="cell-zzyzx-1",
        )
    )
    session.flush()

    feed(
        monkeypatch,
        SyncDiff(
            added=[
                txn("t1", name="ZZYZX ROASTERS 8774471", merchant="zzyzx roasters")
            ],
            modified=[],
            removed=[],
            cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]

    assert row["merchant_key"] == "Zzyzx Roasters", "lowercase from Plaid must snap"
    assert row["suggested_category_id"] == category_id


def test_commit_is_idempotent_on_the_transaction_id(
    client, session, linked, monkeypatch, category_id
):
    _item, account = linked
    payload = {
        "rows": [
            {
                "key": "t1",
                "account_id": account.id,
                "occurred_on": "2026-06-01",
                "raw_description": "HARRIS TEETER 0412",
                "amount": "12.34",
                "category_id": category_id,
            }
        ]
    }
    first = client.post("/api/plaid/commit", json=payload).json()
    second = client.post("/api/plaid/commit", json=payload).json()

    assert first["created"] == 1
    assert second == {"created": 0, "skipped": 1, "errors": []}
    assert (
        session.scalar(
            select(Transaction)
            .where(Transaction.source_ref == "t1")
            .with_only_columns(Transaction.id)
        )
        is not None
    )


def test_committed_rows_carry_the_account(
    client, session, linked, monkeypatch, category_id
):
    """The gap this feature closes: every workbook row has a null account."""
    _item, account = linked
    client.post(
        "/api/plaid/commit",
        json={
            "rows": [
                {
                    "key": "t1",
                    "account_id": account.id,
                    "occurred_on": "2026-06-01",
                    "raw_description": "HARRIS TEETER 0412",
                    "amount": "12.34",
                    "category_id": category_id,
                }
            ]
        },
    )
    row = session.scalar(select(Transaction).where(Transaction.source_ref == "t1"))
    assert row.account_id == account.id
    assert row.source == TransactionSource.LINKED


def test_a_stale_login_is_reported_not_raised(client, session, linked, monkeypatch):
    """One bank needing a re-login must not take the whole refresh down."""
    item, _ = linked

    def explode(token, cursor):
        raise plaid_client.PlaidReauthRequired("ITEM_LOGIN_REQUIRED")

    monkeypatch.setattr(plaid_link, "sync_transactions", explode)
    body = client.post("/api/plaid/sync").json()

    assert body["reauth_needed"] == ["Chase"]
    session.refresh(item)
    assert item.needs_reauth is True


# --- Transfers, income and balances --------------------------------------


def _card(session, item, *, mask, name, account_type="credit"):
    """A second linked account on the same item, to be paid or paid from."""
    account = Account(institution="Chase", name=name, is_retirement=False)
    session.add(account)
    session.flush()
    session.add(
        PlaidAccount(
            item_id=item.id,
            plaid_account_id=f"acct-{mask}",
            account_id=account.id,
            mask=mask,
            account_type=account_type,
        )
    )
    session.flush()
    return account


def test_paying_your_own_card_is_a_transfer_not_spending(
    client, session, linked, monkeypatch
):
    """The case that started this: $207.45 counted twice in one August."""
    item, _ = linked
    _card(session, item, mask="1249", name="Discover it Card")

    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("t1", name="DISCOVER E-PAYMENT 1249 WEB ID: 2510020270",
                       amount="207.45")],
            modified=[], removed=[], cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]

    assert row["suggested_category_name"] == "Transfer"
    assert row["merchant_key"] is None, "a payment to yourself has no merchant"
    assert any("Discover it Card" in n for n in row["notes"])

    category = session.scalars(
        select(Category).where(Category.name == "Transfer")
    ).first()
    assert category.kind == CategoryKind.TRANSFER


def test_a_matching_store_number_is_not_a_transfer(
    client, session, linked, monkeypatch
):
    """The false positive that made a mask alone insufficient.

    The Citi card's real mask is 0412, and HARRIS TEETER 0412 is a real grocery
    descriptor in three years of history. Four digits that happen to match are
    a coincidence; four digits beside the word PAYMENT are a payment.
    """
    item, _ = linked
    _card(session, item, mask="0412", name="Citi Custom Cash")

    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("t1", name="HARRIS TEETER 0412", amount="52.10")],
            modified=[], removed=[], cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]

    assert row["suggested_category_name"] != "Transfer"
    assert not any("payment to your" in n for n in row["notes"])


def test_a_reference_number_containing_the_mask_is_not_a_transfer(
    client, session, linked, monkeypatch
):
    """The mask must stand as its own token, not lurk inside a longer number."""
    item, _ = linked
    _card(session, item, mask="0270", name="Some Card")

    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("t1", name="ACME PAYMENT WEB ID: 2510020270",
                       amount="30.00")],
            modified=[], removed=[], cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]
    assert row["suggested_category_name"] != "Transfer"


def test_money_arriving_in_checking_is_income_not_spending(
    client, session, linked, monkeypatch
):
    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("t1", name="MOODYS PAYROLL DIRECT DEP", amount="-2214.98")],
            modified=[], removed=[], cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]

    assert row["suggested_category_name"] == "Income"
    category = session.scalars(
        select(Category).where(Category.name == "Income")
    ).first()
    assert category.kind == CategoryKind.INCOME


def test_a_refund_on_a_card_is_not_income(client, session, linked, monkeypatch):
    """Negative means opposite things per account type: a deposit in checking,
    a refund on a card — and a refund belongs against what it refunds."""
    session.execute(
        update(PlaidAccount)
        .where(PlaidAccount.plaid_account_id == "acct-1")
        .values(account_type="credit")
    )
    session.flush()

    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("t1", name="UNITED AIRLINES", amount="-500.00")],
            modified=[], removed=[], cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]
    assert row["suggested_category_name"] != "Income"


def test_transfers_and_income_stay_out_of_the_month_total(client, session):
    """The whole point. A card payment must not inflate what you spent.

    The spending category is looked up by kind rather than taken from the
    `category_id` fixture: that returns the first category by sort order, which
    is Savings, and a SAVINGS row was already excluded — so the test passed
    without proving anything about transfers.
    """
    from backend.queries import month_summary
    from backend.routers.transactions import get_or_create_period

    spending = session.scalars(
        select(Category).where(Category.kind == CategoryKind.SPENDING)
    ).first()
    transfer = Category(name="Transfer Test", kind=CategoryKind.TRANSFER, sort_order=99)
    income = Category(name="Income Test", kind=CategoryKind.INCOME, sort_order=100)
    session.add_all([transfer, income])
    session.flush()

    period = get_or_create_period(session, 2026, 9)
    for category, amount in (
        (spending.id, "100.00"),
        (transfer.id, "207.45"),
        (income.id, "-2214.98"),
    ):
        session.add(
            Transaction(
                occurred_on=date(2026, 9, 1),
                period_id=period.id,
                raw_description="x",
                category_id=category,
                amount=Decimal(amount),
                is_recurring=False,
                source=TransactionSource.MANUAL,
            )
        )
    session.flush()

    summary = month_summary(session, 2026, 9, today=date(2026, 9, 30))
    assert summary.spent_total == Decimal("100.00"), (
        "a transfer and a paycheck must not move what you spent"
    )
    split = summary.commitment
    assert split.committed + split.flexible == Decimal("100.00")

    # They are still on screen, just not counted as spending.
    shown = {line.category for line in summary.categories}
    assert "Transfer Test" in shown and "Income Test" in shown


def test_a_charge_from_a_standing_commitment_arrives_committed(
    client, session, linked, monkeypatch, category_id
):
    """Rent and the power bill landed as flexible on the first day of real
    data, because the commit defaults is_recurring to false and nothing
    suggested otherwise. A commitment names its merchant; that is the answer."""
    from backend.models import FixedCost
    from backend.routers.transactions import get_or_create_period

    session.add(
        FixedCost(
            description="Rent (billed as one charge)",
            amount=Decimal("1553.37"),
            category_id=category_id,
            is_exact=False,
            effective_from=date(2026, 1, 1),
            merchant_key="Zzyzx Housing",
        )
    )
    period_txn = Transaction(
        occurred_on=date(2026, 5, 1),
        period_id=get_or_create_period(session, 2026, 5).id,
        raw_description="ZZYZX HOUSING",
        merchant_key="Zzyzx Housing",
        category_id=category_id,
        amount=Decimal("1553.37"),
        is_recurring=True,
        source=TransactionSource.WORKBOOK,
        source_ref="cell-zzyzx-housing",
    )
    session.add(period_txn)
    session.flush()

    feed(
        monkeypatch,
        SyncDiff(
            added=[
                txn("t1", name="ZZYZX HOUSING PPD ID: 1844372402",
                    merchant="Zzyzx Housing", amount="1553.37")
            ],
            modified=[], removed=[], cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]

    assert row["is_recurring"] is True
    assert any("committed" in n for n in row["notes"])


def test_an_ordinary_charge_is_not_marked_committed(
    client, session, linked, monkeypatch
):
    feed(
        monkeypatch,
        SyncDiff(
            added=[txn("t1", name="Uber Eats", merchant="Uber Eats")],
            modified=[], removed=[], cursor="c1",
        ),
    )
    row = client.post("/api/plaid/sync").json()["rows"][0]
    assert row["is_recurring"] is False
