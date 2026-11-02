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

from sqlalchemy import func, select
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
    if start.month == 12:
        return start, date(start.year + 1, 1, 1)
    return start, start.replace(month=start.month + 1)


def days_in(month: date) -> int:
    start, end = month_bounds(month)
    return (end - start).days


def elapsed_in(month: date, today: date | None = None) -> int:
    """How much of this month has actually happened.

    The denominator of every pace figure. A past month is complete; a future
    one has not started; the current one is elapsed through today inclusive, so
    on the 1st you are one day in rather than none — spending on the 1st is not
    infinitely ahead of pace.
    """
    start, end = month_bounds(month)
    # A calendar day, not an instant — see the DTZ011 note in pyproject.
    today = today or date.today()
    if today >= end:
        return (end - start).days
    if today < start:
        return 0
    return today.day


def _to_date(month: date, days_elapsed: int, days: int, budget: Decimal) -> Decimal:
    """What a budget should have spent by now, if spread evenly.

    Deliberately linear. Real spending is not, but the alternative is a model of
    your habits that has to be maintained and that nobody can check — and the
    exact figures sit beside this on screen, so a reader can always see what it
    is a fraction of.
    """
    if not days:
        return ZERO
    return (budget * days_elapsed / days).quantize(Decimal("0.01"))


def month_summary(db: Session, month: date, today: date | None = None) -> dict:
    start, end = month_bounds(month)
    days = days_in(start)
    days_elapsed = elapsed_in(start, today)

    in_month = (
        select(Transaction, Category)
        .join(Category, Transaction.category_id == Category.id)
        .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
    )

    # One pass, bucketed by kind. Summed conditionally rather than filtered,
    # so the transaction count stays honest: a month is still listed at its
    # true number of rows when part of it is transfers.
    totals = dict.fromkeys(CategoryKind, ZERO)
    count = 0
    for txn, category in db.execute(in_month).all():
        totals[category.kind] += txn.amount
        count += 1

    per_category = _category_spend(db, start, end, days_elapsed, days)

    # Spending categories only. Printed as "of X budgeted" beside a total that
    # is spending alone, so counting a savings target here made the two halves
    # of one sentence measure different things.
    budgeted = sum(
        (
            row["budget"] or ZERO
            for row in per_category
            if row["kind"] == CategoryKind.SPENDING
        ),
        ZERO,
    )
    expected_to_date = sum(
        (
            row["expected_to_date"] or ZERO
            for row in per_category
            if row["kind"] == CategoryKind.SPENDING
        ),
        ZERO,
    )
    spent = totals[CategoryKind.SPENDING]

    return {
        "month": start,
        "days_in_month": days,
        "days_elapsed": days_elapsed,
        # What the budget allows by today. None when nothing is budgeted —
        # "you are on track" against no target is not a claim worth making.
        "expected_to_date": expected_to_date if budgeted else None,
        "over_budget": bool(budgeted) and spent > budgeted,
        "over_pace": bool(budgeted) and spent > expected_to_date,
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


def _category_spend(
    db: Session, start: date, end: date, days_elapsed: int = 0, days: int = 0
) -> list[dict]:
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
        expected = (
            None
            if budget is None
            else _to_date(start, days_elapsed, days, budget)
        )
        rows.append(
            {
                "category_id": category.id,
                "category_name": category.name,
                "kind": category.kind,
                "spent": spent,
                "budget": budget,
                "remaining": None if budget is None else budget - spent,
                "expected_to_date": expected,
                # Decided here, in Decimal, rather than by comparing two money
                # strings in the browser. Over budget is a fact about the
                # month; over pace is a fact about today, and a category can be
                # the second without being the first — which is the whole point
                # of showing it mid-month.
                "over_budget": budget is not None and spent > budget,
                "over_pace": expected is not None and spent > expected,
            }
        )
    return rows


def suggest_budgets(db: Session, month: date, months_back: int = 3) -> list[dict]:
    """A starting budget per category, from what you actually spent.

    Budgets went unmaintained for a year in the version this replaced, and
    stayed at zero in this one, for the same reason both times: every month
    starts as seventeen empty boxes and filling them from memory is a task
    nobody does twice.

    The **median** of the last few complete months, not the mean — one flight
    should not set your Travel budget for the year, and with three months a
    single outlier moves a mean by a third of itself.

    Complete months only: the current one is partway through, and including it
    would propose a budget lower than anything you have ever spent.
    """
    this_month, _ = month_bounds(month)

    # Walk back from the month before the one being budgeted.
    periods: list[tuple[date, date]] = []
    cursor = this_month
    for _ in range(months_back):
        cursor = (
            date(cursor.year - 1, 12, 1)
            if cursor.month == 1
            else cursor.replace(month=cursor.month - 1)
        )
        periods.append(month_bounds(cursor))

    per_category: dict[int, list[Decimal]] = {}
    names: dict[int, str] = {}
    for start, end in periods:
        stmt = (
            select(
                Category.id,
                Category.name,
                func.coalesce(func.sum(Transaction.amount), ZERO),
            )
            .join(Transaction, Transaction.category_id == Category.id)
            .where(Category.kind == CategoryKind.SPENDING)
            .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
            .group_by(Category.id, Category.name)
        )
        for category_id, name, total in db.execute(stmt).all():
            per_category.setdefault(category_id, []).append(total)
            names[category_id] = name

    rows = []
    for category_id, amounts in per_category.items():
        # A category that appears in one month of three spent nothing in the
        # other two, and its median has to know that — otherwise a single
        # December flight reads as a monthly habit.
        padded = amounts + [ZERO] * (len(periods) - len(amounts))
        rows.append(
            {
                "category_id": category_id,
                "category_name": names[category_id],
                "amount": _median(padded),
                "months": len(amounts),
            }
        )
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return rows


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))


def net_worth(
    db: Session, limit: int = 365, account_ids: list[int] | None = None
) -> dict:
    """The balance series, one point per day anything reported.

    Liabilities are already negative in the table, so `net` is a plain sum.
    Assets and liabilities are split back out here for display only.

    `accounts_reporting` rides along on every point because it is the one thing
    that makes the series readable: a day when two of eleven accounts were read
    is not a day net worth collapsed, and without the count that dip is
    indistinguishable from a real one.

    Only accounts still tracked are counted as reporting, though every balance
    is summed. The screen judges a day against the best day ever seen, so
    counting a since-closed account would make every day before it closed the
    standard and every day after it look partial — which is exactly what
    hiding Bilt's BillPay account did on 2026-11-02, blanking the chart.

    `account_ids` narrows every figure to those accounts — the card debt over
    time, or the investments alone. None means every account.
    """
    stmt = (
        select(
            Balance.as_of,
            func.sum(func.greatest(Balance.amount, 0)).label("assets"),
            func.sum(func.least(Balance.amount, 0)).label("liabilities"),
            func.sum(Balance.amount).label("net"),
            func.count(Balance.account_id)
            .filter(Account.closed_on.is_(None))
            .label("reporting"),
            func.bool_or(Balance.source == "rebuilt").label("estimated"),
        )
        .join(Account, Account.id == Balance.account_id)
        .group_by(Balance.as_of)
        .order_by(Balance.as_of.desc())
        .limit(limit)
    )
    if account_ids is not None:
        stmt = stmt.where(Balance.account_id.in_(account_ids))

    points = [
        {
            "as_of": row.as_of,
            "assets": row.assets or ZERO,
            "liabilities": row.liabilities or ZERO,
            "net": row.net or ZERO,
            "accounts_reporting": row.reporting,
            "estimated": bool(row.estimated),
        }
        for row in db.execute(stmt).all()
    ]
    points.reverse()
    _attach_breakdown(db, points, account_ids)

    counted = select(func.count(Account.id)).where(Account.closed_on.is_(None))
    if account_ids is not None:
        counted = counted.where(Account.id.in_(account_ids))
    account_count = db.scalar(counted)
    return {
        "points": points,
        "latest": points[-1] if points else None,
        "account_count": account_count or 0,
    }


def _attach_breakdown(
    db: Session, points: list[dict], account_ids: list[int] | None
) -> None:
    """What each day's figure is made of: a subtotal per group, and the
    accounts under it — what the chart's hover shows.

    Summed here, in Decimal, for the reason every total is: the browser has
    no decimal type, and a tooltip whose parts do not add up to its total is
    worse than no tooltip. Each account says whether it was read or rebuilt,
    because an estimate must never be mistaken for a reading at the one place
    someone is looking closely.
    """
    if not points:
        return
    stmt = (
        select(Balance.as_of, Balance.amount, Balance.source, Account)
        .join(Account, Account.id == Balance.account_id)
        .where(Balance.as_of.between(points[0]["as_of"], points[-1]["as_of"]))
    )
    if account_ids is not None:
        stmt = stmt.where(Balance.account_id.in_(account_ids))

    by_day: dict[date, dict[str, list[dict]]] = {}
    for as_of, amount, source, account in db.execute(stmt).all():
        by_day.setdefault(as_of, {}).setdefault(account.group, []).append(
            {
                "account_id": account.id,
                "name": account.name,
                "amount": amount,
                "source": source,
            }
        )

    for point in points:
        groups = by_day.get(point["as_of"], {})
        point["groups"] = [
            {
                "group": group,
                "total": sum((a["amount"] for a in accounts), ZERO),
                "accounts": sorted(accounts, key=lambda a: -abs(a["amount"])),
            }
            for group, accounts in groups.items()
        ]


def portfolio(db: Session, account_ids: list[int] | None = None) -> dict:
    """Holdings as of the most recent snapshot that has any.

    Restricted to one date rather than taking the newest row per security: a
    position sold last month would otherwise carry its final value forward
    forever and keep counting toward the total.

    `account_ids` narrows it to those accounts, as `net_worth` does.
    """
    newest = select(func.max(Holding.as_of))
    if account_ids is not None:
        newest = newest.where(Holding.account_id.in_(account_ids))
    as_of = db.scalar(newest)
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
    if account_ids is not None:
        stmt = stmt.where(Holding.account_id.in_(account_ids))

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
