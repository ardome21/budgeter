"""Refresh, the cursor, and what gets written.

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
from backend.models import (
    Account,
    Balance,
    Holding,
    PlaidCategoryMap,
    PlaidItem,
    Security,
    Transaction,
)
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
    account so a refresh has somewhere to file rows."""

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


def refresh(client: TestClient) -> dict:
    return client.post("/api/linked/refresh").json()


def test_a_refresh_writes_what_the_bank_sends(
    client: TestClient, session: Session, item, fake_plaid
):
    """No review. Four of six banks once sat with a preview parked and not one
    of their transactions in the ledger, because the step that wrote them was a
    click nobody made."""
    fake_plaid(SyncDiff(added=[charge("t1", date(2026, 3, 1), "COFFEE", "4.50")],
                        modified=[], removed=[], cursor="cursor-2"))

    body = refresh(client)

    assert body["added"] == 1
    assert [t.description for t in session.scalars(select(Transaction))] == ["COFFEE"]
    session.refresh(item)
    assert item.cursor == "cursor-2"
    assert item.last_synced_at is not None


def test_a_pending_charge_is_written_and_marked_pending(
    client: TestClient, session: Session, item, fake_plaid
):
    fake_plaid(
        SyncDiff(
            added=[
                charge("t1", date(2026, 3, 1), "COFFEE", "4.50", pending=True),
                charge("t2", date(2026, 3, 1), "LUNCH", "12.00"),
            ],
            modified=[], removed=[], cursor="c",
        )
    )

    refresh(client)

    rows = {t.description: t.pending for t in session.scalars(select(Transaction))}
    assert rows == {"COFFEE": True, "LUNCH": False}


def posted(txn_id: str, pending_id: str, day: date, name: str, amount: str):
    row = charge(txn_id, day, name, amount)
    row.pending_transaction_id = pending_id
    return row


def test_a_posted_charge_takes_over_its_pending_row(
    client: TestClient, session: Session, item, fake_plaid, income_category
):
    """In place, so the coffee is never there twice, and a category chosen
    while it was pending survives the day it posts."""
    fake_plaid(SyncDiff(
        added=[charge("p1", date(2026, 3, 1), "COFFEE PENDING", "4.50", pending=True)],
        modified=[], removed=[], cursor="c1",
    ))
    refresh(client)
    row = session.scalar(select(Transaction))
    row.category_id = income_category.id  # chosen by hand while pending
    row.notes = "with Sam"
    session.flush()

    # Plaid's posting: the posted row points back, the pending id is removed.
    fake_plaid(SyncDiff(
        added=[posted("t1", "p1", date(2026, 3, 3), "COFFEE", "5.40")],
        modified=[], removed=["p1"], cursor="c2",
    ))
    refresh(client)

    rows = session.scalars(select(Transaction)).all()
    assert len(rows) == 1
    (only,) = rows
    assert only.plaid_transaction_id == "t1"
    assert only.pending is False
    assert only.amount == Decimal("5.40")
    assert only.occurred_on == date(2026, 3, 3)
    assert only.category_id == income_category.id
    assert only.notes == "with Sam"


def test_a_pending_charge_the_bank_withdraws_goes(
    client: TestClient, session: Session, item, fake_plaid
):
    """A hold that never posts — a hotel deposit, a cancelled order."""
    fake_plaid(SyncDiff(
        added=[charge("p1", date(2026, 3, 1), "HOTEL HOLD", "200.00", pending=True)],
        modified=[], removed=[], cursor="c1",
    ))
    refresh(client)
    fake_plaid(SyncDiff(added=[], modified=[], removed=["p1"], cursor="c2"))
    refresh(client)

    assert session.scalars(select(Transaction)).all() == []


def test_a_row_is_never_written_twice(
    client: TestClient, session: Session, item, fake_plaid
):
    """A rewind, or a cursor held for a homeless row, re-sends rows already
    written. Identity is Plaid's transaction id."""
    fake_plaid(SyncDiff(added=[charge("t1", date(2026, 3, 1), "COFFEE", "4.50")],
                        modified=[], removed=[], cursor="c"))

    refresh(client)
    item.cursor = None
    session.flush()
    body = refresh(client)

    assert body["added"] == 0
    assert len(session.scalars(select(Transaction)).all()) == 1


def test_a_row_is_filed_by_the_banks_label(
    client: TestClient, session: Session, item, fake_plaid, spending_category
):
    session.add(PlaidCategoryMap(plaid_category="FOOD_AND_DRINK",
                                 category_id=spending_category.id))
    session.flush()
    coffee = charge("t1", date(2026, 3, 1), "COFFEE", "4.50")
    coffee.pfc_primary = "FOOD_AND_DRINK"
    coffee.pfc_detailed = "FOOD_AND_DRINK_COFFEE"
    fake_plaid(SyncDiff(added=[coffee], modified=[], removed=[], cursor="c"))

    refresh(client)

    written = session.scalar(select(Transaction))
    assert written.category_id == spending_category.id
    # Kept verbatim, so a mapping added later can be applied to this row.
    assert written.plaid_category_detailed == "FOOD_AND_DRINK_COFFEE"


def test_a_revision_is_applied_without_asking(
    client: TestClient, session: Session, item, linked_account, fake_plaid,
    spending_category,
):
    """The bank settling its own record. Only the bank's fields move — a
    category changed by hand survives every re-sync."""
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
    refresh(client)

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
    body = refresh(client)

    assert body["removed"] == 1
    assert session.scalars(select(Transaction)).all() == []


def test_a_refresh_records_todays_balance(
    client: TestClient, session: Session, item, linked_account, fake_plaid
):
    """And refreshing twice in one day overwrites rather than accumulates: two
    readings on one date are the same fact measured twice."""
    fake_plaid(SyncDiff(added=[], modified=[], removed=[], cursor="c"))

    refresh(client)
    refresh(client)

    balances = session.scalars(select(Balance)).all()
    assert len(balances) == 1
    assert balances[0].amount == Decimal("1500.00")
    assert balances[0].as_of == date.today()


def test_an_account_takes_the_name_the_bank_shows(
    client: TestClient, session: Session, item, linked_account, monkeypatch,
    fake_plaid,
):
    """Fidelity's `official_name` is "Investment" for every account it has;
    `name` is ROTH IRA. And a rename at the bank reaches an existing row."""
    monkeypatch.setattr(
        plaid_client, "get_accounts",
        lambda _t: [
            LinkedAccount(
                plaid_account_id="acct-1", name="ROTH IRA",
                official_name="Investment", mask="2613", type="investment",
                subtype="roth", current=Decimal("5529.09"),
            )
        ],
    )
    fake_plaid(SyncDiff(added=[], modified=[], removed=[], cursor="c"))

    refresh(client)

    session.refresh(linked_account)
    assert linked_account.name == "ROTH IRA"


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

    refresh(client)

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

    refresh(client)

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

    body = refresh(client)

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

    refresh(client)

    written = session.scalar(
        select(Transaction).where(Transaction.plaid_transaction_id == "t9")
    )
    assert written.merchant == "Netflix"


def test_a_holdings_only_item_is_not_asked_for_transactions(
    client: TestClient, session: Session, item, linked_account, monkeypatch
):
    """An item answers for the products it was created with and no others.

    Fidelity was linked for investments alone, and the refresh called
    /transactions/sync anyway: Plaid answered ADDITIONAL_CONSENT_REQUIRED, the
    endpoint turned that into a 502, and on screen it looked like the refresh
    was broken rather than like a question the item was never granted
    permission to answer.
    """
    item.products = "investments"
    session.flush()

    def explode(*_args):
        raise AssertionError("asked a holdings-only item for transactions")

    monkeypatch.setattr(plaid_client, "sync_transactions", explode)
    monkeypatch.setattr(
        plaid_client, "get_holdings", lambda _t: HoldingsSnapshot()
    )
    monkeypatch.setattr(
        plaid_client, "get_accounts",
        lambda _t: [
            LinkedAccount(
                plaid_account_id="acct-1", name="Brokerage", official_name=None,
                mask="0000", type="investment", subtype="brokerage",
                current=Decimal("1000.00"),
            )
        ],
    )

    body = refresh(client)

    assert body["failures"] == []
    # The balance still lands — that call works for any item.
    assert body["accounts_updated"] == 1


def test_a_row_for_an_unknown_account_does_not_advance_the_cursor(
    client: TestClient, session: Session, item, fake_plaid
):
    """The quietest way this app could lose money.

    A transaction whose account is not on this side cannot be written.
    Advancing the cursor past it would consume a real charge for good. Holding
    it means the next refresh asks again, by which point `_refresh_accounts`
    may well have created the account.
    """
    orphan = charge("t1", date(2026, 3, 1), "COFFEE", "4.50")
    orphan.plaid_account_id = "acct-nobody-knows"
    fake_plaid(SyncDiff(added=[orphan], modified=[], removed=[], cursor="cursor-2"))

    body = refresh(client)

    assert body["added"] == 0
    session.refresh(item)
    assert item.cursor is None


def test_relinking_re_adopts_an_orphaned_account(
    client: TestClient, session: Session, item, linked_account, fake_plaid
):
    """`unlink` nulls `item_id` and leaves the account behind on purpose — its
    balance history is still real. Re-linking has to claim it back, or every
    sync builds an account map that cannot see it and files nothing."""
    linked_account.item_id = None
    session.flush()
    fake_plaid(SyncDiff(added=[charge("t1", date(2026, 3, 1), "COFFEE", "4.50")],
                        modified=[], removed=[], cursor="c"))

    refresh(client)

    session.refresh(linked_account)
    assert linked_account.item_id == item.id
    assert [t.description for t in session.scalars(select(Transaction))] == ["COFFEE"]


def test_one_bank_failing_does_not_take_the_others_down(
    client: TestClient, session: Session, item, linked_account, monkeypatch
):
    """A single 502 for the lot hides which bank is the problem, and makes a
    stale login look like the whole app being broken."""
    second = PlaidItem(
        item_id="item-2", institution_id="ins_2", institution_name="Other Bank",
        access_token=plaid_client.encrypt_token("access-sandbox-2"),
        products="transactions", sync_start_on=date(2024, 1, 1),
        linked_at=datetime.now(UTC),
    )
    session.add(second)
    session.flush()

    monkeypatch.setattr(
        plaid_client, "get_accounts",
        lambda _t: [
            LinkedAccount(
                plaid_account_id="acct-1", name="Test Checking",
                official_name=None, mask="0000", type="depository",
                subtype="checking", current=Decimal("1500.00"),
            )
        ],
    )

    def sync(_token, cursor):
        if _token == "access-sandbox-2":
            raise plaid_client.PlaidReauthRequired("login gone stale")
        return SyncDiff(
            added=[charge("t1", date(2026, 3, 1), "COFFEE", "4.50")],
            modified=[], removed=[], cursor="c",
        )

    monkeypatch.setattr(plaid_client, "sync_transactions", sync)

    body = refresh(client)

    assert body["added"] == 1
    assert len(body["failures"]) == 1
    assert "Other Bank" in body["failures"][0]
    session.refresh(second)
    assert second.needs_reauth is True


def test_refreshing_twice_in_a_day_leaves_one_reading(
    client: TestClient, session: Session, item, linked_account, monkeypatch
):
    """A balance is keyed (account, day): two readings on one date are the same
    fact measured twice, not two facts. That is what makes this safe to run on
    a schedule and again the moment the app is opened."""
    monkeypatch.setattr(
        plaid_client, "get_accounts",
        lambda _t: [
            LinkedAccount(
                plaid_account_id="acct-1", name="Test Checking",
                official_name=None, mask="0000", type="depository",
                subtype="checking", current=Decimal("1500.00"),
            )
        ],
    )

    monkeypatch.setattr(
        plaid_client, "sync_transactions",
        lambda *_: SyncDiff(added=[], modified=[], removed=[], cursor="c"),
    )

    refresh(client)
    refresh(client)

    rows = session.scalars(
        select(Balance).where(Balance.account_id == linked_account.id)
    ).all()
    assert len(rows) == 1


def test_each_bank_lists_its_accounts_and_how_current_they_are(
    client: TestClient, session: Session, item, linked_account, add_transaction,
    spending_category,
):
    """A bank's last sync says nothing about whether every account came in.
    Capital One synced daily while never sending a $1,030.57 payment."""
    add_transaction(account=linked_account, category=spending_category,
                    amount="5.00", occurred_on=date(2025, 3, 1))
    add_transaction(account=linked_account, category=spending_category,
                    amount="7.00", occurred_on=date(2025, 4, 9))

    bank = next(i for i in client.get("/api/linked/items").json() if i["id"] == item.id)
    acct = bank["accounts"][0]

    assert acct["name"] == "Test Checking"
    assert acct["oldest_transaction"] == "2025-03-01"
    assert acct["newest_transaction"] == "2025-04-09"
    assert acct["transaction_count"] == 2
    assert acct["new_this_week"] == 2
