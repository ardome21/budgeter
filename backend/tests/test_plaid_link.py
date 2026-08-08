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
from sqlalchemy import select

from backend import plaid_client
from backend.models import (
    Account,
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
            subtype="credit card",
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
