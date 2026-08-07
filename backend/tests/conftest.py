"""Test fixtures.

Every test runs inside a transaction that is rolled back afterwards, against
the real development database. That keeps the tests honest — they exercise
Postgres, real constraints and real enum types rather than a SQLite
approximation — while leaving no residue behind.

`join_transaction_mode="create_savepoint"` is what makes it work: endpoint code
calls session.commit() freely, each commit releases a SAVEPOINT rather than
ending the outer transaction, and the final rollback still undoes all of it.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db import engine, get_session
from backend.main import app
from backend.models import Category


@pytest.fixture
def session() -> Generator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield db
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def client(session: Session) -> Generator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def category_id(session: Session) -> int:
    """Any real category id, so tests do not depend on a particular seed."""
    cat = session.query(Category).order_by(Category.sort_order).first()
    if cat is None:
        pytest.skip("no categories in the database — run the importer first")
    return cat.id
