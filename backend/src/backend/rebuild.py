"""Filling in the days nobody read, from what is known about them.

Plaid reports a balance as of today and nothing earlier, so a net-worth line
grows one point per day the app is opened. This fills the gaps behind it, in
two ways, because there are two kinds of account:

**Cash and cards** are rebuilt from their transactions. A balance on any day is
the next *reading* after it, with every transaction in between put back:
`balance(d) = reading(r) + sum(amount for d < occurred_on <= r)`, Plaid's sign
convention being positive for money out. Anchoring each gap on the nearest
later reading rather than on today keeps one bad stretch from carrying
backwards for months. Checked against the real 2026-09-18 readings before this
was written: five accounts of six to the cent, and Chase off by one $248.32
debit dated the 17th that the bank had not posted by the 18th — a day's lag
in the date, not an error that accumulates.

Each rebuild starts at the first transaction the bank has sent for that item,
because before it the history is not missing — it is unknown.

**Investments** are not rebuilt from anything: their value moves with the
market, and Plaid reports no prices for past days. Days *between* two readings
are drawn as a straight line between them, which is an estimate and is stored
as one. Before an account's first reading, its first reading is held flat back
to the earliest day any investment account was read — so a small account that
was only linked later (the ESPP, $132.84) does not leave every earlier day
short of an account. After its last reading there is nothing to rebuild.

Every row written here is `source = "rebuilt"`, written only on a day with no
reading, and all of them are deleted and recomputed on each run. A reading is
never touched.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import Account, Balance, Transaction

REBUILT = "rebuilt"
CENT = Decimal("0.01")


def rebuild_balances(db: Session) -> int:
    """Recompute every rebuilt balance. Returns how many rows were written."""
    db.execute(delete(Balance).where(Balance.source == REBUILT))
    db.flush()

    readings: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for account_id, as_of, amount in db.execute(
        select(Balance.account_id, Balance.as_of, Balance.amount)
    ):
        readings[account_id][as_of] = amount

    accounts = db.scalars(
        select(Account).where(Account.closed_on.is_(None))
    ).all()

    investments = [a for a in accounts if a.account_type == "investment"]
    first_invested = min(
        (min(readings[a.id]) for a in investments if readings[a.id]),
        default=None,
    )

    written = 0
    for account in accounts:
        known = readings[account.id]
        if not known:
            continue
        if account.account_type == "investment":
            rows = _between_readings(known, first_invested)
        else:
            rows = _from_transactions(db, account, known)
        for as_of, amount in rows.items():
            db.add(
                Balance(
                    account_id=account.id,
                    as_of=as_of,
                    amount=amount,
                    source=REBUILT,
                )
            )
            written += 1

    db.commit()
    return written


def _from_transactions(
    db: Session, account: Account, known: dict[date, Decimal]
) -> dict[date, Decimal]:
    """Cash and cards: each unread day from the next reading, transactions put
    back."""
    start = _history_starts(db, account)
    if start is None:
        return {}

    daily: dict[date, Decimal] = dict(
        db.execute(
            select(Transaction.occurred_on, func.sum(Transaction.amount))
            .where(Transaction.account_id == account.id)
            # Rebuilt from posted rows only, as it was before pending rows
            # were kept: the anchor reading is the bank's posted balance.
            .where(Transaction.pending.is_(False))
            .group_by(Transaction.occurred_on)
        ).all()
    )

    rows: dict[date, Decimal] = {}
    # Walked backwards from the last reading. `running` is the balance at the
    # end of the day being visited; stepping to the day before puts that day's
    # transactions back.
    day = max(known)
    running = known[day]
    while day > start:
        running += daily.get(day, Decimal(0))
        day -= timedelta(days=1)
        if day in known:
            running = known[day]
        else:
            rows[day] = running
    return rows


def _history_starts(db: Session, account: Account) -> date | None:
    """The first day the bank sent anything for this account's item.

    Per item rather than per account: a savings account that earns interest
    once a month has nothing in its own history for weeks, which is not the
    same as its history being unknown.
    """
    siblings = (
        select(Account.id).where(Account.item_id == account.item_id)
        if account.item_id is not None
        else select(Account.id).where(Account.id == account.id)
    )
    return db.scalar(
        select(func.min(Transaction.occurred_on)).where(
            Transaction.account_id.in_(siblings)
        )
    )


def _between_readings(
    known: dict[date, Decimal], first_invested: date | None
) -> dict[date, Decimal]:
    """Investments: a straight line between readings, and the first reading
    held back to when the others were first read."""
    dates = sorted(known)
    rows: dict[date, Decimal] = {}

    for earlier, later in pairwise(dates):
        span = (later - earlier).days
        step = (known[later] - known[earlier]) / span
        for n in range(1, span):
            estimate = known[earlier] + step * n
            rows[earlier + timedelta(days=n)] = estimate.quantize(CENT)

    if first_invested is not None:
        day = first_invested
        while day < dates[0]:
            rows[day] = known[dates[0]]
            day += timedelta(days=1)
    return rows
