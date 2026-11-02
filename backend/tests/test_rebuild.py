"""Rebuilding the days nobody read. See `backend.rebuild`."""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Account, Balance, Transaction
from backend.rebuild import rebuild_balances


def _reading(session, account, day, amount, source="bank"):
    session.add(
        Balance(account_id=account.id, as_of=day, amount=Decimal(amount), source=source)
    )


def _spend(session, account, category, day, amount):
    session.add(
        Transaction(
            account_id=account.id, category_id=category.id, occurred_on=day,
            amount=Decimal(amount), description="X", created_at=datetime.now(UTC),
        )
    )


def _series(session, account) -> dict[date, tuple[str, str]]:
    return {
        b.as_of: (str(b.amount), b.source)
        for b in session.scalars(
            select(Balance).where(Balance.account_id == account.id)
        )
    }


def test_cash_is_rebuilt_backwards_from_the_next_reading(
    session: Session, account, spending_category
):
    """Positive is money out, so stepping back a day puts it back."""
    _spend(session, account, spending_category, date(2026, 3, 1), "10.00")
    _spend(session, account, spending_category, date(2026, 3, 3), "25.00")
    _spend(session, account, spending_category, date(2026, 3, 4), "-100.00")
    _reading(session, account, date(2026, 3, 4), "500.00")
    session.flush()

    rebuild_balances(session)

    assert _series(session, account) == {
        date(2026, 3, 1): ("425.00", "rebuilt"),
        date(2026, 3, 2): ("425.00", "rebuilt"),
        date(2026, 3, 3): ("400.00", "rebuilt"),
        date(2026, 3, 4): ("500.00", "bank"),
    }


def test_nothing_is_rebuilt_before_the_bank_sent_anything(
    session: Session, account, spending_category
):
    _spend(session, account, spending_category, date(2026, 3, 3), "25.00")
    _reading(session, account, date(2026, 3, 4), "500.00")
    session.flush()

    rebuild_balances(session)

    assert min(_series(session, account)) == date(2026, 3, 3)


def test_a_reading_is_never_overwritten_and_reruns_are_safe(
    session: Session, account, spending_category
):
    """Each gap anchors on the reading after it, so an archived reading in the
    middle stands and the days before it follow from it."""
    _spend(session, account, spending_category, date(2026, 3, 1), "5.00")
    _reading(session, account, date(2026, 3, 2), "300.00", source="archive")
    _reading(session, account, date(2026, 3, 4), "500.00")
    session.flush()

    rebuild_balances(session)
    rebuild_balances(session)

    series = _series(session, account)
    assert series[date(2026, 3, 2)] == ("300.00", "archive")
    assert series[date(2026, 3, 1)] == ("300.00", "rebuilt")
    assert series[date(2026, 3, 3)] == ("500.00", "rebuilt")
    assert len(series) == 4


def test_investments_are_drawn_straight_between_readings(session: Session):
    """No transactions explain a market move, so the gap is an estimate — and a
    late-linked account is held at its first reading back to the others'."""
    roth, espp = (
        Account(
            plaid_account_id=f"test-{name}", name=name, institution="Fidelity",
            account_type="investment", subtype=subtype, is_liability=False,
        )
        for name, subtype in (("Roth", "roth"), ("ESPP", "stock plan"))
    )
    session.add_all([roth, espp])
    session.flush()
    _reading(session, roth, date(2026, 3, 1), "100.00")
    _reading(session, roth, date(2026, 3, 5), "140.00")
    _reading(session, espp, date(2026, 3, 3), "7.00")
    session.flush()

    rebuild_balances(session)

    roth_series = _series(session, roth)
    assert roth_series[date(2026, 3, 2)] == ("110.00", "rebuilt")
    assert roth_series[date(2026, 3, 4)] == ("130.00", "rebuilt")
    assert _series(session, espp) == {
        date(2026, 3, 1): ("7.00", "rebuilt"),
        date(2026, 3, 2): ("7.00", "rebuilt"),
        date(2026, 3, 3): ("7.00", "bank"),
    }
