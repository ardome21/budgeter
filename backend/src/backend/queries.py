"""Everything the app reports, derived on read.

Nothing in here is stored. The workbook this app replaced kept its rollups as
cells, computed by SUMIF over a hardcoded list of category names, and any
category missing from that list silently vanished from the total — two real
cases, one understating a month by $28.94 and one making disposable income
optimistic by exactly $89.00. A total that is a query cannot drift from the
rows it totals.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from .models import (
    Account,
    Balance,
    Budget,
    Category,
    CategoryKind,
    Holding,
    Security,
    Transaction,
)

ZERO = Decimal("0.00")


def month_bounds(month: date) -> tuple[date, date]:
    """First of the month, and first of the next. Half-open, so a transaction
    on the last day at any hour belongs to exactly one month."""
    start = month.replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def spending_only(stmt: Select) -> Select:
    """The rule behind every figure that claims you spent something.

    This exists as a function, used everywhere, because when it was written out
    by hand at each call site it ended up correct in three places and absent in
    two. A $207.45 credit card payment was excluded from "spent this month" and
    from the committed/flexible split, and then showed up as the second largest
    purchase of the month and in the period list's total — because those two
    totals were written before TRANSFER existed and never learned about it.

    A card payment is not spending. It is last month's spending moving between
    two accounts you own.
    """
    return stmt.where(Category.kind == CategoryKind.SPENDING)


def month_summary(db: Session, month: date) -> dict:
    start, end = month_bounds(month)

    in_month = (
        select(Transaction, Category)
        .join(Category, Transaction.category_id == Category.id)
        .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
    )

    # One pass, bucketed by kind. Summed conditionally rather than filtered,
    # so the transaction count stays honest: a month is still listed at its
    # true number of rows when part of it is transfers.
    totals = {kind: ZERO for kind in CategoryKind}
    count = 0
    for txn, category in db.execute(in_month).all():
        totals[category.kind] += txn.amount
        count += 1

    per_category = _category_spend(db, start, end)
    budgeted = sum((row["budget"] or ZERO for row in per_category), ZERO)

    return {
        "month": start,
        "spent": totals[CategoryKind.SPENDING],
        # Income arrives negative in Plaid's convention (money entering the
        # account). Flipped once, here, so every caller sees a positive number
        # for money coming in.
        "income": -totals[CategoryKind.INCOME],
        "saved": totals[CategoryKind.SAVINGS],
        "transferred": totals[CategoryKind.TRANSFER],
        "budgeted": budgeted,
        "categories": per_category,
        "transaction_count": count,
    }


def _category_spend(db: Session, start: date, end: date) -> list[dict]:
    """Every category, with what it cost and what it was meant to cost.

    Left join from categories, not from transactions: a category with a budget
    and no spending is exactly the row worth seeing, and an inner join hides it.
    """
    spend = (
        select(
            Transaction.category_id,
            func.coalesce(func.sum(Transaction.amount), ZERO).label("spent"),
        )
        .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
        .group_by(Transaction.category_id)
        .subquery()
    )
    budgets = (
        select(Budget.category_id, Budget.amount)
        .where(Budget.month == start)
        .subquery()
    )

    stmt = (
        select(
            Category,
            func.coalesce(spend.c.spent, ZERO),
            budgets.c.amount,
        )
        .outerjoin(spend, spend.c.category_id == Category.id)
        .outerjoin(budgets, budgets.c.category_id == Category.id)
        .order_by(Category.sort_order, Category.name)
    )

    rows = []
    for category, spent, budget in db.execute(stmt).all():
        if spent == ZERO and budget is None:
            # A category nobody used and nobody budgeted is noise on a screen
            # that is trying to say where the money went.
            continue
        rows.append(
            {
                "category_id": category.id,
                "category_name": category.name,
                "kind": category.kind,
                "spent": spent,
                "budget": budget,
                "remaining": None if budget is None else budget - spent,
            }
        )
    return rows


def net_worth(db: Session, limit: int = 365) -> dict:
    """The balance series, one point per day anything reported.

    Liabilities are already negative in the table, so `net` is a plain sum.
    Assets and liabilities are split back out here for display only.

    `accounts_reporting` rides along on every point because it is the one thing
    that makes the series readable: a day when two of eleven accounts were read
    is not a day net worth collapsed, and without the count that dip is
    indistinguishable from a real one.
    """
    stmt = (
        select(
            Balance.as_of,
            func.sum(func.greatest(Balance.amount, 0)).label("assets"),
            func.sum(func.least(Balance.amount, 0)).label("liabilities"),
            func.sum(Balance.amount).label("net"),
            func.count(Balance.account_id).label("reporting"),
        )
        .group_by(Balance.as_of)
        .order_by(Balance.as_of.desc())
        .limit(limit)
    )

    points = [
        {
            "as_of": row.as_of,
            "assets": row.assets or ZERO,
            "liabilities": row.liabilities or ZERO,
            "net": row.net or ZERO,
            "accounts_reporting": row.reporting,
        }
        for row in db.execute(stmt).all()
    ]
    points.reverse()

    account_count = db.scalar(
        select(func.count(Account.id)).where(Account.closed_on.is_(None))
    )
    return {
        "points": points,
        "latest": points[-1] if points else None,
        "account_count": account_count or 0,
    }


def portfolio(db: Session) -> dict:
    """Holdings as of the most recent snapshot that has any.

    Restricted to one date rather than taking the newest row per security: a
    position sold last month would otherwise carry its final value forward
    forever and keep counting toward the total.
    """
    as_of = db.scalar(select(func.max(Holding.as_of)))
    if as_of is None:
        return {
            "as_of": None,
            "total_value": ZERO,
            "total_cost_basis": None,
            "holdings": [],
        }

    stmt = (
        select(Holding, Security, Account)
        .join(Security, Holding.security_id == Security.id)
        .join(Account, Holding.account_id == Account.id)
        .where(Holding.as_of == as_of)
        .order_by(Holding.value.desc())
    )

    holdings = []
    total_value = ZERO
    total_basis = ZERO
    # What the basis total actually accounts for. A "paid in" figure is only
    # readable next to how much of the portfolio it covers — and here it covers
    # 23% of it, because the employer-plan trust holding three quarters of the
    # money reports no basis at all.
    covered_value = ZERO

    for holding, security, account in db.execute(stmt).all():
        total_value += holding.value
        basis = _basis(holding, security)

        if basis is not None:
            total_basis += basis
            covered_value += holding.value

        holdings.append(
            {
                "security_id": security.id,
                "name": security.name,
                "ticker": security.ticker,
                "kind": security.kind,
                "is_cash_equivalent": security.is_cash_equivalent,
                "account_id": account.id,
                "account_name": account.name,
                "quantity": holding.quantity,
                "price": holding.price,
                "value": holding.value,
                "cost_basis": basis,
                "gain": None if basis is None else holding.value - basis,
            }
        )

    return {
        "as_of": as_of,
        "total_value": total_value,
        "total_cost_basis": total_basis if covered_value else None,
        "cost_basis_covers": covered_value,
        "holdings": holdings,
    }


def _basis(holding, security) -> Decimal | None:
    """What was paid in, with the one rule an institution cannot state for you.

    A money market fund and a cash balance are worth $1.00 a unit by
    construction, so their cost basis *is* their value — Fidelity reports null
    because there is nothing to report, not because the figure is unknown.
    Treating that null as unknown is what nulled the cost basis of half the
    accounts and the headline growth figure with them, the last time this was
    computed.

    Derived here and never written back: the stored row keeps the institution's
    null, so nothing in the table is ever an inference of ours.
    """
    if holding.cost_basis is not None:
        return holding.cost_basis
    if security.is_cash_equivalent:
        return holding.value
    return None


def months_with_data(db: Session) -> list[date]:
    """Every month that has a transaction, newest first."""
    # Built once and reused for the select, the group and the order. Written
    # out twice, Postgres sees two separately-parameterised expressions and
    # refuses to match the ORDER BY against the GROUP BY.
    first_of_month = func.date_trunc("month", Transaction.occurred_on)
    stmt = (
        select(first_of_month.label("m"))
        .group_by(first_of_month)
        .order_by(first_of_month.desc())
    )
    return [row.m.date() for row in db.execute(stmt).all()]
