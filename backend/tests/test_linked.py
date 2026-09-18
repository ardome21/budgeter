"""Preview, commit, and the cursor.

Nothing here touches the network. `sync_transactions`, `get_accounts` and
`get_holdings` are the three seams every test replaces, which is what makes it
possible to assert on the rules — the cursor discipline, the identity of a row,
what is applied silently — rather than on Plaid being up.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import plaid_client
from backend.models import Account, Balance, Holding, PlaidItem, Security, Transaction
from backend.plaid_client import (
    HeldPosition,
    HeldSecurity,
    HoldingsSnapshot,
    LinkedAccount,
    LinkedTransaction,
    SyncDiff,
)


@pytest.fixture
def item(session: Session) -> PlaidItem:
    created = PlaidItem(
        item_id="item-1",
        institution_id="ins_1",
        institution_name="Test Bank",
        access_token=plaid_client.encrypt_token("access-sandbox-test"),
        products="transactions",
        sync_start_on=date(2024, 1, 1),
        linked_at=datetime.now(UTC),
    )
    session.add(created)
    session.flush()
    return created


@pytest.fixture
def linked_account(session: Session, item: PlaidItem) -> Account:
    account = Account(
        plaid_account_id="acct-1",
        item_id=item.id,
        name="Test Checking",
        institution="Test Bank",
        account_type="depository",
        subtype="checking",
        is_liability=False,
    )
    session.add(account)
    session.flush()
    return account


def charge(txn_id: str, day: date, name: str, amount: str, pending: bool = False):
    return LinkedTransaction(
        transaction_id=txn_id,
        plaid_account_id="acct-1",
        occurred_on=day,
        name=name,
        merchant_name=None,
        amount=Decimal(amount),
        pending=pending,
        category=None,
    )


@pytest.fixture
def fake_plaid(monkeypatch, linked_account):
    """Replace the three network calls. `get_accounts` returns the fixture
    account so a preview has somewhere to file rows."""

    def accounts(_token):
        return [
            LinkedAccount(
                plaid_account_id="acct-1",
                name="Test Checking",
                official_name=None,
                mask="0000",
                type="depository",
                subtype="checking",
                current=Decimal("1500.00"),
            )
        ]

    monkeypatch.setattr(plaid_client, "get_accounts", accounts)

    def set_sync(diff: SyncDiff):
        monkeypatch.setattr(plaid_client, "sync_transactions", lambda *_: diff)

    return set_sync


def test_the_cursor_does_not_move_on_a_preview(
    client: TestClient, session: Session, item, fake_plaid
):
    """The rule everything else rests on. Abandoning a preview has to re-offer
    the same rows next time rather than step past them for good — getting this
    wrong loses real transactions with nothing on screen to say so."""
    fake_plaid(SyncDiff(added=[charge("t1", date(2026, 3, 1), "COFFEE", "4.50")],
                        modified=[], removed=[], cursor="cursor-2"))

    body = client.post(f"/api/linked/items/{item.id}/preview").json()

    assert len(body["rows"]) == 1
    assert body["cursor_advanced"] is False
    session.refresh(item)
    assert item.cursor is None
    assert item.pending_cursor == "cursor-2"


def test_an_empty_sync_advances_immediately(
    client: TestClient, session: Session, item, fake_plaid
):
    """Otherwise a rewind that re-offers nothing repeats forever."""
    fake_plaid(SyncDiff(added=[], modified=[], removed=[], cursor="cursor-2"))

    body = client.post(f"/api/linked/items/{item.id}/preview").json()

    assert body["cursor_advanced"] is True
    session.refresh(item)
    assert item.cursor == "cursor-2"


def test_pending_charges_are_never_offered(
    client: TestClient, item, fake_plaid
):
    """They get withdrawn and re-sent when they post, with a new date, often a
    new amount and usually a rewritten descriptor. Offering them means
    reviewing the same coffee twice."""
    fake_plaid(
        SyncDiff(
            added=[
                charge("t1", date(2026, 3, 1), "COFFEE", "4.50", pending=True),
                charge("t2", date(2026, 3, 1), "LUNCH", "12.00"),
            ],
            modified=[], removed=[], cursor="c",
        )
    )

    rows = client.post(f"/api/linked/items/{item.id}/preview").json()["rows"]

    assert [r["description"] for r in rows] == ["LUNCH"]


def test_an_unticked_row_is_a_rejection_the_cursor_passes(
    client: TestClient, session: Session, item, fake_plaid
):
    """Not an oversight to be re-offered forever. `rewind` is the escape hatch
    when it was a mistake."""
    fake_plaid(
        SyncDiff(
            added=[
                charge("t1", date(2026, 3, 1), "KEEP", "10.00"),
                charge("t2", date(2026, 3, 2), "DROP", "20.00"),
            ],
            modified=[], removed=[], cursor="cursor-2",
        )
    )
    client.post(f"/api/linked/items/{item.id}/preview")

    client.post("/api/linked/commit", json={"item_id": item.id, "accept": ["t1"]})

    session.refresh(item)
    assert item.cursor == "cursor-2"
    assert item.pending_cursor is None
    written = session.scalars(select(Transaction)).all()
    assert [t.description for t in written] == ["KEEP"]


def test_committed_rows_are_not_offered_again(
    client: TestClient, session: Session, item, fake_plaid
):
    """A rewind re-offers everything the bank has; only what is genuinely
    missing should come back. Identity is Plaid's transaction id."""
    diff = SyncDiff(added=[charge("t1", date(2026, 3, 1), "COFFEE", "4.50")],
                    modified=[], removed=[], cursor="c")
    fake_plaid(diff)
    client.post(f"/api/linked/items/{item.id}/preview")
    client.post("/api/linked/commit", json={"item_id": item.id, "accept": ["t1"]})

    client.post(f"/api/linked/items/{item.id}/rewind")
    rows = client.post(f"/api/linked/items/{item.id}/preview").json()["rows"]

    assert rows == []


def test_a_revision_is_applied_without_asking(
    client: TestClient, session: Session, item, linked_account, fake_plaid,
    spending_category,
):
    """The bank settling its own record is not a suggestion. Holding it behind
    a prompt leaves the ledger knowingly wrong until somebody clicks."""
    session.add(
        Transaction(
            plaid_transaction_id="t1",
            account_id=linked_account.id,
            category_id=spending_category.id,
            occurred_on=date(2026, 3, 1),
            amount=Decimal("4.50"),
            description="PENDING COFFEE",
            merchant="Corner Cafe",
            created_at=datetime.now(UTC),
        )
    )
    session.flush()

    fake_plaid(
        SyncDiff(
            added=[], removed=[], cursor="c",
            modified=[charge("t1", date(2026, 3, 3), "CORNER CAFE 001", "5.25")],
        )
    )
    client.post(f"/api/linked/items/{item.id}/preview")

    revised = session.scalar(
        select(Transaction).where(Transaction.plaid_transaction_id == "t1")
    )
    assert revised.amount == Decimal("5.25")
    assert revised.occurred_on == date(2026, 3, 3)
    # The user's own answer to the row survives the bank's correction.
    assert revised.merchant == "Corner Cafe"
    assert revised.category_id == spending_category.id


def test_a_removal_deletes_the_row(
    client: TestClient, session: Session, item, linked_account, fake_plaid,
    spending_category,
):
    session.add(
        Transaction(
            plaid_transaction_id="t1",
            account_id=linked_account.id,
            category_id=spending_category.id,
            occurred_on=date(2026, 3, 1),
            amount=Decimal("4.50"),
            description="WITHDRAWN",
            created_at=datetime.now(UTC),
        )
    )
    session.flush()

    fake_plaid(SyncDiff(added=[], modified=[], removed=["t1"], cursor="c"))
    body = client.post(f"/api/linked/items/{item.id}/preview").json()

    assert body["removed"] == 1
    assert session.scalars(select(Transaction)).all() == []


def test_a_refresh_records_todays_balance(
    client: TestClient, session: Session, item, linked_account, fake_plaid
):
    """And refreshing twice in one day overwrites rather than accumulates: two
    readings on one date are the same fact measured twice."""
    fake_plaid(SyncDiff(added=[], modified=[], removed=[], cursor="c"))

    client.post(f"/api/linked/items/{item.id}/preview")
    client.post(f"/api/linked/items/{item.id}/preview")

    balances = session.scalars(select(Balance)).all()
    assert len(balances) == 1
    assert balances[0].amount == Decimal("1500.00")
    assert balances[0].as_of == date.today()


def test_a_card_balance_is_stored_negative(
    client: TestClient, session: Session, item, monkeypatch, fake_plaid
):
    """Plaid reports $207.45 owed as +207.45. Net worth is a plain sum, so the
    sign is flipped once on the way in — not in every query that reads it."""
    monkeypatch.setattr(
        plaid_client, "get_accounts",
        lambda _t: [
            LinkedAccount(
                plaid_account_id="card-1", name="Test Card", official_name=None,
                mask="1111", type="credit", subtype="credit card",
                current=Decimal("207.45"),
            )
        ],
    )
    fake_plaid(SyncDiff(added=[], modified=[], removed=[], cursor="c"))

    client.post(f"/api/linked/items/{item.id}/preview")

    account = session.scalar(
        select(Account).where(Account.plaid_account_id == "card-1")
    )
    assert account.is_liability is True
    balance = session.scalar(select(Balance).where(Balance.account_id == account.id))
    assert balance.amount == Decimal("-207.45")


def test_holdings_are_only_fetched_for_an_investments_item(
    client: TestClient, session: Session, item, monkeypatch, fake_plaid
):
    """An item created for transactions alone cannot answer the holdings call.
    Asking anyway is an error that must not take the transaction sync with it."""
    called = []
    monkeypatch.setattr(
        plaid_client, "get_holdings",
        lambda _t: called.append(1) or HoldingsSnapshot(),
    )
    fake_plaid(SyncDiff(added=[], modified=[], removed=[], cursor="c"))

    client.post(f"/api/linked/items/{item.id}/preview")

    assert called == []


def test_holdings_land_priced_by_the_institution(
    client: TestClient, session: Session, item, linked_account, monkeypatch,
    fake_plaid,
):
    """The whole reason the investments code shrank. No quote is fetched, no
    proxy stands in for an unquotable trust, and nothing is inferred — the
    institution's own price and value are what get stored."""
    item.products = "transactions,investments"
    session.flush()

    monkeypatch.setattr(
        plaid_client, "get_holdings",
        lambda _t: HoldingsSnapshot(
            securities=[
                HeldSecurity(
                    security_id="sec-1", name="FID FRDM INX 2065", ticker=None,
                    cusip="31564E540", type="mutual fund",
                )
            ],
            holdings=[
                HeldPosition(
                    plaid_account_id="acct-1", security_id="sec-1",
                    quantity=Decimal("5460.762"), price=Decimal("22.7000"),
                    value=Decimal("123959.30"), cost_basis=None,
                    as_of=date(2026, 3, 1),
                )
            ],
        ),
    )
    fake_plaid(SyncDiff(added=[], modified=[], removed=[], cursor="c"))

    body = client.post(f"/api/linked/items/{item.id}/preview").json()

    assert body["holdings_updated"] == 1
    holding = session.scalar(select(Holding))
    assert holding.value == Decimal("123959.30")
    assert holding.price == Decimal("22.7000")
    # Not reported, so not invented. Zero would claim the lot as gain.
    assert holding.cost_basis is None
    security = session.scalar(select(Security))
    assert security.cusip == "31564E540"


def test_a_merchant_joins_the_spelling_already_in_use(
    client: TestClient, session: Session, item, linked_account, fake_plaid,
    add_transaction, spending_category,
):
    """The prefix rule, which is what replaced a merge queue. `NETFLIX.COM`
    normalises to 'Netflix Com'; an existing 'Netflix' is a whole-word prefix
    of it, so the row joins the name already on file."""
    add_transaction(
        account=linked_account, category=spending_category, amount="12.99",
        occurred_on=date(2026, 1, 1), merchant="Netflix",
    )
    fake_plaid(
        SyncDiff(added=[charge("t9", date(2026, 3, 1), "NETFLIX.COM", "12.99")],
                 modified=[], removed=[], cursor="c")
    )

    rows = client.post(f"/api/linked/items/{item.id}/preview").json()["rows"]

    assert rows[0]["merchant"] == "Netflix"
    # And the category it was filed under last time is offered with it.
    assert rows[0]["category_id"] == spending_category.id
