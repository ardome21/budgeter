"""Test fixtures.

Every test runs inside a transaction that is rolled back afterwards, against
the real development database. That keeps the tests honest — they exercise
Postgres, real constraints and real enum types rather than a SQLite
approximation — while leaving no residue behind.

`join_transaction_mode="create_savepoint"` is what makes it work: endpoint code
calls session.commit() freely, each commit releases a SAVEPOINT rather than
ending the outer transaction, and the final rollback still undoes all of it.

`autoflush=False` matters just as much, and is easy to leave off: it is what
SessionLocal uses, and Session() defaults the other way. With the default on,
a pending insert is visible to the next query and endpoint code that forgets to
flush passes here and raises a unique violation in production. That is exactly
how the import endpoint came to 500 on two identical rows in one file while its
tests were green.
"""

from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.db import engine, get_session
from backend.main import app
from backend.models import (
    Account,
    AppUser,
    Balance,
    Budget,
    Category,
    CategoryKind,
    Goal,
    Holding,
    PayStub,
    PlaidCategoryMap,
    PlaidItem,
    Security,
    Transaction,
)


@pytest.fixture
def session() -> Generator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,  # as SessionLocal builds it
    )
    try:
        yield db
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def client(session: Session) -> Generator[TestClient]:
    """A client with the API open, as it is before anyone has set up a login.

    The user rows are cleared inside the test transaction — not committed, so
    the developer's real login survives the rollback untouched. Without this
    the whole suite depends on whether the machine it runs on happens to have
    a login configured: setting one up turned 78 of these red, because the
    guard correctly refused every request.

    Tests that care about the guard create their own user; see test_auth.py.
    """
    # Deleted through the ORM, one object at a time, so the relationship
    # cascades take the recovery codes and passkeys with them. A bulk
    # `delete(AppUser)` is plain SQL and skips those cascades: the day a real
    # passkey existed, its foreign key blocked the delete and every test using
    # this fixture errored at setup. Doing it this way needs no maintenance
    # when the next table hangs off a user.
    for user in session.scalars(select(AppUser)).all():
        session.delete(user)

    # And from any bank actually linked on this machine. A test that syncs
    # would otherwise iterate the developer's real institutions alongside its
    # own fixture: `reauth_needed == ["Chase"]` became four institutions, and
    # one revision was applied five times. Same reasoning as the users above —
    # inside the transaction, so nothing real is lost to the rollback.
    for item in session.scalars(select(PlaidItem)).all():
        session.delete(item)

    session.flush()

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _empty_ledger(session: Session) -> None:
    """Clear the real data before each test, inside the rolled-back transaction.

    These tests run against the development database, which now fills itself
    from five live banks. A month total, a net-worth series or a "biggest
    holding" assertion computed over the developer's actual spending is a test
    that passes on this machine and nowhere else — and, worse, one that starts
    failing on a Tuesday because a refresh ran.

    This is the same lesson the holdings tests learned twice: deleted here, not
    committed, so the real rows come back with the rollback.
    """
    session.execute(delete(Holding))
    session.execute(delete(Balance))
    session.execute(delete(Transaction))
    session.execute(delete(Budget))
    session.execute(delete(PayStub))
    session.execute(delete(Goal))
    session.execute(delete(Security))
    session.execute(delete(Account))
    # And the seeded Plaid category map. It is real configuration on this
    # machine — 26 rows from scripts/seed_category_map.py — so a test asserting
    # "FOOD_AND_DRINK files as Food and Drinks" would be asserting on the
    # developer's seed rather than on the resolver, and would break the day
    # that seed was edited. Each test states the mapping it depends on.
    session.execute(delete(PlaidCategoryMap))
    session.flush()


@pytest.fixture
def category_id(session: Session) -> int:
    """Any real category id, so tests do not depend on a particular seed."""
    cat = session.query(Category).order_by(Category.sort_order).first()
    if cat is None:
        pytest.skip("no categories — run scripts/seed_categories.py")
    return cat.id


@pytest.fixture
def spending_category(session: Session) -> Category:
    return _category(session, CategoryKind.SPENDING, "Groceries")


@pytest.fixture
def transfer_category(session: Session) -> Category:
    return _category(session, CategoryKind.TRANSFER, "Transfer")


@pytest.fixture
def income_category(session: Session) -> Category:
    return _category(session, CategoryKind.INCOME, "Income")


def _category(session: Session, kind: CategoryKind, name: str) -> Category:
    existing = session.scalar(select(Category).where(Category.kind == kind))
    if existing is not None:
        return existing
    created = Category(name=name, kind=kind, sort_order=0)
    session.add(created)
    session.flush()
    return created


@pytest.fixture
def account(session: Session) -> Account:
    created = Account(
        plaid_account_id="test-account",
        name="Test Checking",
        institution="Test Bank",
        account_type="depository",
        subtype="checking",
        is_liability=False,
    )
    session.add(created)
    session.flush()
    return created


@pytest.fixture
def add_transaction(session: Session):
    """Write one transaction. A fixture rather than an importable helper,
    because `tests` is not a package and making it one to share three lines is
    a worse trade than passing the factory in."""

    def _add(
        *,
        account: Account,
        category: Category,
        amount: str,
        occurred_on: date,
        description: str = "TEST CHARGE",
        merchant: str | None = None,
        plaid_transaction_id: str | None = None,
    ) -> Transaction:
        txn = Transaction(
            plaid_transaction_id=plaid_transaction_id,
            account_id=account.id,
            category_id=category.id,
            occurred_on=occurred_on,
            amount=Decimal(amount),
            description=description,
            merchant=merchant,
            created_at=datetime.now(UTC),
        )
        session.add(txn)
        session.flush()
        return txn

    return _add
