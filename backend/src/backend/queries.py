"""Everything the app reports, derived on read.

Nothing in here is stored. The workbook this app replaced kept its rollups as
cells, computed by SUMIF over a hardcoded list of category names, and any
category missing from that list silently vanished from the total — two real
cases, one understating a month by $28.94 and one making disposable income
optimistic by exactly $89.00. A total that is a query cannot drift from the
rows it totals.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Account,
    Balance,
    Budget,
    Category,
    CategoryKind,
    Holding,
    MerchantRule,
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


def previous_month(month: date) -> date:
    start = month.replace(day=1)
    if start.month == 1:
        return date(start.year - 1, 12, 1)
    return start.replace(month=start.month - 1)


def budget_source(db: Session, month: date) -> date | None:
    """The month whose budget is in force for `month`: its own, or else the
    most recent one set before it.

    A budget stays until it is changed. Every month used to start empty, and
    budgets went unmaintained for a year in the version this replaced for that
    reason alone — a set you have to re-enter monthly is a set nobody keeps.
    A month's budget is one set, saved whole, so it rolls forward whole: a
    category left out of the latest set has no budget, not an older one.
    """
    return db.scalar(
        select(func.max(Budget.month)).where(Budget.month <= month.replace(day=1))
    )


def month_summary(db: Session, month: date, today: date | None = None) -> dict:
    start, end = month_bounds(month)
    days = days_in(start)
    days_elapsed = elapsed_in(start, today)
    source = budget_source(db, start)

    in_month = (
        select(Transaction, Category)
        .join(Category, Transaction.category_id == Category.id)
        .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
    )

    # One pass, bucketed by kind. Summed conditionally rather than filtered,
    # so the transaction count stays honest: a month is still listed at its
    # true number of rows when part of it is transfers.
    # Pending rows are reported apart and counted in none of the totals: the
    # amount still moves when it posts, and "spent" means what has posted.
    totals = dict.fromkeys(CategoryKind, ZERO)
    count = 0
    pending_total, pending_count = ZERO, 0
    for txn, category in db.execute(in_month).all():
        if txn.pending:
            if category.kind == CategoryKind.SPENDING:
                pending_total += txn.amount
                pending_count += 1
            continue
        totals[category.kind] += txn.amount
        count += 1

    per_category = _category_spend(db, start, end, days_elapsed, days, source)

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
    income = -totals[CategoryKind.INCOME]

    # What you expect to be paid, from the income budget in force. Stored
    # positive, the way it is typed; None when no income has been budgeted.
    expected = [
        row["budget"]
        for row in per_category
        if row["kind"] == CategoryKind.INCOME and row["budget"] is not None
    ]
    income_expected = sum(expected, ZERO) if expected else None

    # Against last month, and to the same day while this one is running:
    # comparing the 2nd of November with the whole of October says November is
    # cheap, every month, until about the 25th.
    last = previous_month(start)
    last_start, last_end = month_bounds(last)
    through = None if days_elapsed >= days else min(days_elapsed, days_in(last))
    compare_end = last_end if through is None else last_start + timedelta(days=through)
    before = _spending_by_category(db, last_start, compare_end)
    for row in per_category:
        if row["kind"] == CategoryKind.SPENDING:
            row["previous"] = before.pop(row["category_id"], ZERO)
            row["change"] = row["spent"] - row["previous"]
    previous_spent = _spending_total(db, last_start, compare_end)

    merchants = _top_merchants(db, start, end)
    for row in per_category:
        row["merchants"] = merchants.get(row["category_id"], [])

    upcoming = upcoming_charges(db, start, today) if days_elapsed < days else []
    upcoming_total = sum((u["amount"] for u in upcoming), ZERO)
    left_now = (
        income - spent if income_expected is None
        else max(income, income_expected) - spent
    )

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
        "income": income,
        "income_expected": income_expected,
        # Money in less spending, as it stands. Early in the month, before
        # payday, this is negative and says nothing alarming.
        "left": income - spent,
        # What is still yours this month once the expected pay lands: the
        # larger of expected and received, so a bonus counts and a late
        # paycheck does not read as a shortfall.
        "left_expected": (
            None if income_expected is None
            else max(income, income_expected) - spent
        ),
        # What the plan itself leaves: expected pay less the spending budget.
        "left_planned": (
            None if income_expected is None or not budgeted
            else income_expected - budgeted
        ),
        "saved": totals[CategoryKind.SAVINGS],
        "transferred": totals[CategoryKind.TRANSFER],
        "budgeted": budgeted,
        # Which month's budget this is — this one's, or one carried forward.
        "budget_from": source,
        "categories": per_category,
        "transaction_count": count,
        "previous_month": last,
        # None when the whole of last month is the comparison.
        "compared_through": through,
        "previous_spent": previous_spent,
        "daily": _cumulative(db, start, end, days_elapsed),
        "previous_daily": _cumulative(db, last_start, last_end, days_in(last)),
        "biggest": _biggest(db, start, end),
        "upcoming": upcoming,
        "upcoming_total": upcoming_total,
        # Spending authorised but not posted.
        "pending_total": pending_total,
        "pending_count": pending_count,
        # What is left once what is pending has posted and the regular
        # charges still to come have landed.
        "left_after_upcoming": left_now - pending_total - upcoming_total,
    }


def _spending(start: date, end: date, include_pending: bool = False):
    """Spending rows in [start, end). Every "you spent this" figure below
    starts here, so a card payment can never wander into one of them — nor a
    pending charge, whose amount is not settled yet."""
    stmt = (
        select(Transaction)
        .join(Category, Transaction.category_id == Category.id)
        .where(Category.kind == CategoryKind.SPENDING)
        .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
    )
    return stmt if include_pending else stmt.where(Transaction.pending.is_(False))


def _spending_total(db: Session, start: date, end: date) -> Decimal:
    rows = _spending(start, end).subquery()
    return db.scalar(select(func.coalesce(func.sum(rows.c.amount), ZERO)))


def _spending_by_category(db: Session, start: date, end: date) -> dict[int, Decimal]:
    rows = _spending(start, end).subquery()
    return dict(
        db.execute(
            select(rows.c.category_id, func.sum(rows.c.amount)).group_by(
                rows.c.category_id
            )
        ).all()
    )


def _cumulative(db: Session, start: date, end: date, through: int) -> list[Decimal]:
    """Spending so far at the end of each day, from day 1 through `through`.

    One figure per day, including the days nothing was spent, so the line is
    flat where the month was quiet rather than skipping over it.
    """
    rows = _spending(start, end).subquery()
    by_day = dict(
        db.execute(
            select(rows.c.occurred_on, func.sum(rows.c.amount)).group_by(
                rows.c.occurred_on
            )
        ).all()
    )
    running, out = ZERO, []
    for offset in range(min(through, (end - start).days)):
        running += by_day.get(start + timedelta(days=offset), ZERO)
        out.append(running)
    return out


def _merchant_of(txn: Transaction) -> str:
    return txn.merchant or txn.description


def _top_merchants(
    db: Session, start: date, end: date, per_category: int = 3
) -> dict[int, list[dict]]:
    """The few merchants that make up most of each category this month."""
    totals: dict[int, dict[str, list]] = {}
    for txn in db.scalars(_spending(start, end)):
        entry = totals.setdefault(txn.category_id, {}).setdefault(
            _merchant_of(txn), [ZERO, 0]
        )
        entry[0] += txn.amount
        entry[1] += 1
    return {
        category_id: [
            {"name": name, "spent": spent, "count": n}
            for name, (spent, n) in sorted(
                names.items(), key=lambda kv: kv[1][0], reverse=True
            )[:per_category]
        ]
        for category_id, names in totals.items()
    }


def _biggest(db: Session, start: date, end: date, limit: int = 5) -> list[dict]:
    stmt = (
        _spending(start, end)
        .order_by(Transaction.amount.desc(), Transaction.occurred_on)
        .limit(limit)
    )
    return [
        {
            "id": txn.id,
            "occurred_on": txn.occurred_on,
            "merchant": _merchant_of(txn),
            "category_name": txn.category.name,
            "amount": txn.amount,
        }
        for txn in db.scalars(stmt)
    ]


# How alike two charges have to be to count as the same bill: within a fifth
# of the amount, and within a week of the same day of the month. Loose enough
# for a streaming price rise and rent that moves by $50; tight enough that a
# restaurant you happen to visit monthly does not pass, because its amount and
# its day both wander. A bill whose amount swings more than that (an electric
# bill: $85 to $286 in two months) is not predicted, and is not guessed at.
SAME_AMOUNT = Decimal("0.20")
SAME_DAY = 7
# And a bill comes from a merchant you pay once a month, or twice (Apple bills
# two subscriptions separately). Somewhere you shop ten times a month always
# has some charge that lines up with last month's by chance; on real data
# that made the supermarket three "bills".
MOST_A_MONTH = 2


def _alike(a: Transaction, b: Transaction) -> bool:
    return (
        abs(a.amount - b.amount) <= a.amount * SAME_AMOUNT
        and abs(a.occurred_on.day - b.occurred_on.day) <= SAME_DAY
    )


def regular_charges(db: Session, month: date) -> list[Transaction]:
    """The charges that recur, as they last came: one row from the month
    before `month` for each, that has a match in each of the two before it.

    Worked out from the rows, not stored: a charge that stops recurring stops
    being regular after one month without anyone cancelling anything.
    """
    start = month.replace(day=1)
    months = [previous_month(start)]
    for _ in range(2):
        months.append(previous_month(months[-1]))
    history = [
        list(db.scalars(_spending(*month_bounds(m)).where(Transaction.amount > 0)))
        for m in months
    ]

    found: list[Transaction] = []
    for charge in history[0]:
        key = _merchant_of(charge)
        if any(
            sum(_merchant_of(t) == key for t in month_rows) > MOST_A_MONTH
            for month_rows in history
        ):
            continue
        regular = all(
            any(_merchant_of(t) == key and _alike(charge, t) for t in older)
            for older in history[1:]
        )
        if not regular:
            continue
        # One per bill, even when last month charged it twice.
        if any(_merchant_of(f) == key and _alike(charge, f) for f in found):
            continue
        found.append(charge)
    return found


def regular_list(db: Session, month: date) -> list[dict]:
    """The regular charges for the Budget screen, each with where it is
    filed and whether that is your rule or the bank's label."""
    rules = {r.merchant: r.category_id for r in db.scalars(select(MerchantRule))}
    rows = [
        {
            "merchant": _merchant_of(c),
            "amount": c.amount,
            "day": c.occurred_on.day,
            "category_id": c.category_id,
            "category_name": c.category.name,
            "ruled": _merchant_of(c) in rules,
        }
        for c in regular_charges(db, month)
    ]
    rows.sort(key=lambda r: (r["category_name"], r["merchant"], r["amount"]))
    return rows


def upcoming_charges(
    db: Session, month: date, today: date | None = None
) -> list[dict]:
    """Regular charges that have not come yet this month.

    Each is expected for the amount it was last month, on the day it came last
    month — a price rise shows up the first month it lands.
    """
    start, end = month_bounds(month)
    today = today or date.today()
    # Pending counts as arrived: rent that is pending is not still to come.
    this_month = list(db.scalars(_spending(start, end, include_pending=True)))

    found = []
    for charge in regular_charges(db, start):
        key = _merchant_of(charge)
        # Already here this month — at any amount near it, on any day.
        if any(
            _merchant_of(t) == key
            and abs(t.amount - charge.amount) <= charge.amount * SAME_AMOUNT
            for t in this_month
        ):
            continue
        day = min(charge.occurred_on.day, (end - start).days)
        expected_on = start.replace(day=day)
        found.append(
            {
                "merchant": key,
                "category_name": charge.category.name,
                "amount": charge.amount,
                "expected_on": expected_on,
                # Its day has passed and it has not posted: late, or pending,
                # or cancelled. Said rather than dropped, because which of the
                # three is yours to know.
                "late": expected_on < today,
            }
        )
    found.sort(key=lambda f: (f["expected_on"], f["merchant"]))
    return found


def _category_spend(
    db: Session,
    start: date,
    end: date,
    days_elapsed: int = 0,
    days: int = 0,
    source: date | None = None,
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
        .where(Transaction.pending.is_(False))
        .group_by(Transaction.category_id)
        .subquery()
    )
    budgets = (
        select(Budget.category_id, Budget.amount)
        .where(Budget.month == source)
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


def suggest_budgets(
    db: Session,
    month: date,
    months_back: int = 3,
    kind: CategoryKind = CategoryKind.SPENDING,
) -> list[dict]:
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
            .where(Category.kind == kind)
            .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
            .where(Transaction.pending.is_(False))
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


def budget_plan(db: Session, month: date) -> dict:
    """Everything the Budget screen needs to set a month in one sitting.

    Every spending category, each with the budget in force now, what it cost
    last month, and what it typically costs (the median of three, from
    `suggest_budgets`). Both proposals are rounded **up** to the next $10: a
    target reads as a decision rather than a receipt, and rounding down would
    set a budget you had already broken last month. Nothing spent proposes
    nothing — a blank, not a $0 target.

    Income categories come back separately, as `income`, so the screen can set
    the plan against what you expect to be paid and say what it leaves.
    Nothing is written; the screen saves through `PUT /budgets`.
    """
    start = month.replace(day=1)
    last = previous_month(start)
    source = budget_source(db, start)

    last_spend = {
        row["category_id"]: row["spent"]
        for row in _category_spend(db, *month_bounds(last))
    }
    typical = {row["category_id"]: row for row in suggest_budgets(db, start)}
    budgets = {
        b.category_id: b.amount
        for b in db.scalars(select(Budget).where(Budget.month == source))
    }

    rows = []
    for category in db.scalars(
        select(Category)
        .where(Category.kind == CategoryKind.SPENDING)
        .order_by(Category.sort_order, Category.name)
    ):
        spent = last_spend.get(category.id, ZERO)
        usual = typical.get(category.id)
        rows.append(
            {
                "category_id": category.id,
                "category_name": category.name,
                "budget": budgets.get(category.id),
                "last_month": spent,
                "typical": usual["amount"] if usual else ZERO,
                "months_seen": usual["months"] if usual else 0,
                "from_last_month": _round(spent, ROUND_CEILING),
                "from_typical": (
                    _round(usual["amount"], ROUND_CEILING) if usual else None
                ),
            }
        )
    # Biggest first, by what last month cost: the rows worth deciding on lead.
    rows.sort(key=lambda r: r["last_month"], reverse=True)

    # Money in, on the same terms but the other way up: stored and shown
    # positive, and rounded **down**, because an income plan you fall short of
    # spends money that never arrived.
    usual_income = {
        row["category_id"]: row
        for row in suggest_budgets(db, start, kind=CategoryKind.INCOME)
    }
    income_rows = []
    for category in db.scalars(
        select(Category)
        .where(Category.kind == CategoryKind.INCOME)
        .order_by(Category.sort_order, Category.name)
    ):
        received = -last_spend.get(category.id, ZERO)
        usual = usual_income.get(category.id)
        typical_in = -usual["amount"] if usual else ZERO
        income_rows.append(
            {
                "category_id": category.id,
                "category_name": category.name,
                "budget": budgets.get(category.id),
                "last_month": received,
                "typical": typical_in,
                "months_seen": usual["months"] if usual else 0,
                "from_last_month": _round(received, ROUND_FLOOR),
                "from_typical": _round(typical_in, ROUND_FLOOR),
            }
        )

    summary = month_summary(db, last)
    return {
        "month": start,
        "budget_from": source,
        "last_month": last,
        "last_month_spent": summary["spent"],
        "last_month_income": summary["income"],
        "typical_spent": sum((r["typical"] for r in rows), ZERO),
        "rows": rows,
        "income": income_rows,
    }


def _round(
    amount: Decimal, rounding: str, step: Decimal = Decimal(10)
) -> Decimal | None:
    """To a multiple of $10, up for spending ($842.17 → $850.00) and down for
    income ($4,957.23 → $4,950.00). Nothing (or a month of refunds) proposes
    nothing."""
    if amount <= 0:
        return None
    return ((amount / step).to_integral_value(rounding) * step).quantize(
        Decimal("0.01")
    )


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
