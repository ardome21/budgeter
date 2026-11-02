"""Where you stand, and how money moves through a month.

Two halves. **Standing** is what you have today, by kind of money: cash you
can spend, investments you could sell, retirement money you cannot touch yet,
and what is owed. **Flow** is each month as earned, spent and kept.

The bank sees take-home pay only. What a paycheck loses before it lands —
tax, a 401(k), an HSA — comes from the pay stub (`PayStub`), entered by hand
and carried forward. Without it, the savings rate counts only what is left in
the bank, and reads far lower than it is.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .models import (
    Account,
    Balance,
    Category,
    CategoryKind,
    Contribution,
    PayLineKind,
    PayStub,
    Transaction,
)
from .queries import ZERO, _spending_total, month_bounds, previous_month

# A bank whose history starts this many days into a month still counts the
# month as whole. Bilt's starts 2026-06-03; a charge in the first two days is
# rarer than a June that shows as partial for no visible reason.
GRACE = timedelta(days=7)

# How many finished months the averages are taken over.
TYPICAL_MONTHS = 3


def stub_for(db: Session, month: date) -> PayStub | None:
    """The stub in force in `month`: the latest one starting on or before it."""
    return db.scalar(
        select(PayStub)
        .where(PayStub.effective_from <= month)
        .order_by(PayStub.effective_from.desc())
        .options(selectinload(PayStub.lines))
        .limit(1)
    )


def stub_totals(stub: PayStub) -> dict:
    """Per paycheck: each kind summed, and the take-home they leave."""
    by_kind = dict.fromkeys(PayLineKind, ZERO)
    for line in stub.lines:
        by_kind[line.kind] += line.amount
    taken = by_kind[PayLineKind.TAX] + by_kind[PayLineKind.SAVING]
    taken += by_kind[PayLineKind.DEDUCTION]
    return {
        "tax": by_kind[PayLineKind.TAX],
        "saving": by_kind[PayLineKind.SAVING],
        "deduction": by_kind[PayLineKind.DEDUCTION],
        "employer": by_kind[PayLineKind.EMPLOYER],
        "take_home": stub.gross - taken,
    }


def history_start(db: Session) -> date | None:
    """The first day every bank has history for.

    Each bank's history starts on its own day. Until the last of them has
    started, a month is missing a bank's worth of spending, and reporting it
    as whole would make it look cheap.
    """
    firsts = (
        select(func.min(Transaction.occurred_on).label("first"))
        .join(Account, Account.id == Transaction.account_id)
        .group_by(Account.item_id)
        .subquery()
    )
    return db.scalar(select(func.max(firsts.c.first)))


def _flow_month(db: Session, month: date, today: date, starts: date | None) -> dict:
    start, end = month_bounds(month)

    income = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), ZERO))
        .join(Category, Category.id == Transaction.category_id)
        .where(Category.kind == CategoryKind.INCOME)
        .where(Transaction.pending.is_(False))
        .where(Transaction.occurred_on >= start, Transaction.occurred_on < end)
    )
    # Plaid's sign: money in is negative. Flipped once, here.
    take_home = -income
    spent = _spending_total(db, start, end)
    kept = take_home - spent
    row = {
        "month": start,
        "partial": end > today,
        "missing_history": starts is None or starts > start + GRACE,
        "take_home": take_home,
        "spent": spent,
        "kept": kept,
        "has_stub": False,
        "earned": None,
        "tax": None,
        "saved_from_pay": None,
        "employer": None,
        "deductions": None,
        "saved": kept,
        "savings_rate": None,
    }

    stub = stub_for(db, start)
    if stub is not None:
        per_check = stub_totals(stub)
        # Paychecks a month, on average. 24 a year is exactly two; 26 is not a
        # whole number, and averaging is truer than counting deposits, which
        # cannot tell a paycheck from a refund.
        each = Decimal(stub.periods_per_year) / 12
        if row["partial"]:
            # A month still running has only had the paychecks that landed.
            # Counting a whole month's 401(k) on the 2nd, before any pay, made
            # November look like a 44% savings month with nothing earned.
            expected = per_check["take_home"] * each
            if expected > 0:
                each *= min(Decimal(1), max(ZERO, take_home / expected))

        def monthly(amount: Decimal) -> Decimal:
            return (amount * each).quantize(Decimal("0.01"))

        tax = monthly(per_check["tax"])
        saved_from_pay = monthly(per_check["saving"])
        deductions = monthly(per_check["deduction"])
        measured = _measured_employer(db, stub, start, end, each)
        row["employer_by_account"] = measured
        employer = monthly(per_check["employer"]) + sum(measured.values(), ZERO)
        # Gross is worked back from what actually arrived, not taken from the
        # stub, so interest and a bonus count and a missed paycheck shows.
        earned = take_home + tax + saved_from_pay + deductions
        saved = kept + saved_from_pay + employer
        row.update(
            has_stub=True,
            earned=earned,
            tax=tax,
            saved_from_pay=saved_from_pay,
            employer=employer,
            deductions=deductions,
            saved=saved,
        )
        whole = earned + employer
        if whole > 0:
            row["savings_rate"] = (saved / whole).quantize(Decimal("0.001"))
    elif take_home > 0:
        row["savings_rate"] = (kept / take_home).quantize(Decimal("0.001"))
    return row


# How long a plan can take to post a paycheck's contribution. Fidelity posted
# the 2026-07-31 paycheck's on 2026-08-03, which counted July short and
# August a whole paycheck over.
POSTING_LAG = timedelta(days=5)


def _paid_in(
    db: Session, account_ids: list[int], start: date, end: date
) -> dict[int, Decimal]:
    """What went into each account for this month's pay.

    A contribution belongs to the month of the money-in it follows: the latest
    deposit to the bank at most `POSTING_LAG` before it, or its own day when
    there is none. So a paycheck's contribution posted on the 3rd counts with
    the paycheck, and one made on its own day counts where it lands.
    """
    rows = db.execute(
        select(Contribution.account_id, Contribution.occurred_on, Contribution.amount)
        .where(Contribution.account_id.in_(account_ids))
        .where(
            Contribution.occurred_on >= start,
            Contribution.occurred_on < end + POSTING_LAG,
        )
    ).all()
    pay_days = sorted(
        db.scalars(
            select(Transaction.occurred_on)
            .join(Category, Category.id == Transaction.category_id)
            .where(Category.kind == CategoryKind.INCOME)
            .where(Transaction.occurred_on >= start - POSTING_LAG)
            .where(Transaction.occurred_on < end + POSTING_LAG)
            .distinct()
        )
    )
    paid: dict[int, Decimal] = {}
    for account_id, on, amount in rows:
        follows = [d for d in pay_days if on - POSTING_LAG <= d <= on]
        belongs = follows[-1] if follows else on
        if start <= belongs < end:
            paid[account_id] = paid.get(account_id, ZERO) + amount
    return paid


def _measured_employer(
    db: Session, stub: PayStub, start: date, end: date, each: Decimal
) -> dict[str, Decimal]:
    """The employer's money in each plan a stub line pays into, this month.

    Everything paid into the plan, less your own lines into it. The plan
    does not say whose a contribution is; the stub says how much was yours,
    and the rest — the match on each paycheck, and the contributions made
    on days that are not paydays — can only be the employer's.

    Never negative: a contribution posted a day into next month makes this
    month look short, and the employer does not take money back.
    """
    yours: dict[int, Decimal] = {}
    for line in stub.lines:
        if line.kind == PayLineKind.SAVING and line.account_id is not None:
            yours[line.account_id] = yours.get(line.account_id, ZERO) + line.amount
    if not yours:
        return {}
    # Retirement plans only. An ESPP line points at the brokerage it buys
    # into, so the goals know that money is reachable; nobody else pays in
    # there, and it is not a plan with an employer's share to measure.
    names = dict(
        db.execute(
            select(Account.id, Account.name)
            .where(Account.id.in_(list(yours)))
            .where(Account.is_retirement.is_(True))
        ).all()
    )
    yours = {k: v for k, v in yours.items() if k in names}
    if not yours:
        return {}
    paid = _paid_in(db, list(yours), start, end)
    return {
        names[account_id]: max(
            ZERO,
            paid.get(account_id, ZERO) - (amount * each).quantize(Decimal("0.01")),
        )
        for account_id, amount in yours.items()
    }


# The order the breakdown reads in: what comes out, then what is added.
BREAKDOWN_ORDER = (
    PayLineKind.TAX,
    PayLineKind.SAVING,
    PayLineKind.DEDUCTION,
    PayLineKind.EMPLOYER,
)


def pay_breakdown(
    db: Session, typical: list[dict], take_home: Decimal | None
) -> dict | None:
    """How a typical month's pay adds up, line by line, in monthly amounts.

    Only when every typical month has a stub, and from the stub in force in
    the newest of them. Each line is the stub's per-paycheck amount times
    paychecks a month; subtotals are the sum of the lines shown, so the
    screen adds up as printed. Whatever reached the bank beyond the stub's
    take-home — savings interest, a cent of rounding — is its own line rather
    than a gap: on 2026-11-02 the user went looking for the $26.57 between
    two paychecks' gross and "Earned".
    """
    if not typical or take_home is None or not all(f["has_stub"] for f in typical):
        return None
    stub = stub_for(db, typical[0]["month"])
    each = Decimal(stub.periods_per_year) / 12

    def monthly(amount: Decimal) -> Decimal:
        return (amount * each).quantize(Decimal("0.01"))

    groups = []
    for kind in BREAKDOWN_ORDER:
        lines = [
            {"name": line.name, "amount": monthly(line.amount)}
            for line in stub.lines
            if line.kind == kind
        ]
        lines.sort(key=lambda line: -line["amount"])
        if lines:
            total = sum((line["amount"] for line in lines), ZERO)
            groups.append({"kind": kind, "total": total, "lines": lines})

    # The employer's money measured from the plan, averaged like everything
    # else in a typical month, beside any fixed line typed on the stub.
    measured: dict[str, Decimal] = {}
    for f in typical:
        for name, amount in f.get("employer_by_account", {}).items():
            measured[name] = measured.get(name, ZERO) + amount
    if measured:
        lines = [
            {
                "name": f"Paid into {name}",
                "amount": (total / len(typical)).quantize(Decimal("0.01")),
            }
            for name, total in measured.items()
        ]
        employer = next((g for g in groups if g["kind"] == PayLineKind.EMPLOYER), None)
        if employer is None:
            employer = {"kind": PayLineKind.EMPLOYER, "total": ZERO, "lines": []}
            groups.append(employer)
        employer["lines"] += lines
        employer["total"] = sum((line["amount"] for line in employer["lines"]), ZERO)

    gross = monthly(stub.gross)
    taken = sum((g["total"] for g in groups if g["kind"] != PayLineKind.EMPLOYER), ZERO)
    take_home_pay = gross - taken
    other = take_home - take_home_pay
    return {
        "paychecks_per_month": each.quantize(Decimal("0.01")),
        "gross_per_paycheck": stub.gross,
        "gross": gross,
        "groups": groups,
        "take_home_pay": take_home_pay,
        "other_income": other,
        "reached_bank": take_home,
        "earned": gross + other,
    }


def _typical_saved(average) -> Decimal | None:
    """Saved, typically — but only counting the paycheck's part when every
    typical month has a stub. Otherwise it averages two months that count a
    401(k) with one that does not, and sets that over take-home alone: a stub
    entered from September read as a 31% savings rate on 2026-11-02."""
    return average("saved") if average("earned") is not None else average("kept")


def _rate(average) -> Decimal | None:
    """The typical savings rate, from the typical figures rather than an
    average of rates, so a thin month does not weigh as much as a full one."""
    saved, earned = _typical_saved(average), average("earned")
    if earned is not None:
        whole = earned + (average("employer") or ZERO)
        return (saved / whole).quantize(Decimal("0.001")) if whole > 0 else None
    take_home = average("take_home")
    if take_home and take_home > 0 and saved is not None:
        return (saved / take_home).quantize(Decimal("0.001"))
    return None


def _latest_balances(db: Session) -> list[tuple[Account, Decimal, date]]:
    newest = (
        select(Balance.account_id, func.max(Balance.as_of).label("as_of"))
        .group_by(Balance.account_id)
        .subquery()
    )
    stmt = (
        select(Account, Balance.amount, Balance.as_of)
        .join(Balance, Balance.account_id == Account.id)
        .join(
            newest,
            (newest.c.account_id == Balance.account_id)
            & (newest.c.as_of == Balance.as_of),
        )
        .where(Account.closed_on.is_(None))
    )
    return [(a, amount, as_of) for a, amount, as_of in db.execute(stmt).all()]


# Which standing bucket each `Account.group` falls in, in display order.
BUCKETS = {
    "cash": ("checking", "savings"),
    "invested": ("investment", "other"),
    "retirement": ("retirement",),
    "owed": ("credit", "loan"),
}


def overview(db: Session, months: int = 12, today: date | None = None) -> dict:
    today = today or date.today()  # noqa: DTZ011
    starts = history_start(db)

    # Newest month first, back to the first month there is any history for.
    flow = []
    month = today.replace(day=1)
    earliest = starts.replace(day=1) if starts else month
    while month >= earliest and len(flow) < months:
        flow.append(_flow_month(db, month, today, starts))
        month = previous_month(month)

    whole = [f for f in flow if not f["partial"] and not f["missing_history"]]
    typical = whole[:TYPICAL_MONTHS]

    def average(key: str) -> Decimal | None:
        values = [f[key] for f in typical if f[key] is not None]
        if not values or len(values) < len(typical):
            return None
        return (sum(values, ZERO) / len(values)).quantize(Decimal("0.01"))

    buckets = dict.fromkeys(BUCKETS, ZERO)
    accounts = []
    for account, amount, as_of in _latest_balances(db):
        bucket = next(b for b, groups in BUCKETS.items() if account.group in groups)
        buckets[bucket] += amount
        accounts.append(
            {
                "account_id": account.id,
                "name": account.name,
                "institution": account.institution,
                "bucket": bucket,
                "amount": amount,
                "as_of": as_of,
            }
        )
    accounts.sort(key=lambda a: (list(BUCKETS).index(a["bucket"]), -abs(a["amount"])))

    avg_spent = average("spent")
    # Cash after the cards are paid off: the card balance is spending already
    # done, and counting it as spendable cash would flatter the cushion.
    cushion = buckets["cash"] + buckets["owed"]
    months_covered = (
        (cushion / avg_spent).quantize(Decimal("0.1"))
        if avg_spent and avg_spent > 0
        else None
    )

    return {
        "as_of": max((a["as_of"] for a in accounts), default=None),
        "net_worth": sum(buckets.values(), ZERO),
        "cash": buckets["cash"],
        "invested": buckets["invested"],
        "retirement": buckets["retirement"],
        "owed": buckets["owed"],
        "accounts": accounts,
        "cushion": cushion,
        "months_covered": months_covered,
        "typical_months": [f["month"] for f in typical],
        "typical": {
            "take_home": average("take_home"),
            "spent": avg_spent,
            "kept": average("kept"),
            "earned": average("earned"),
            "saved": _typical_saved(average),
            "saved_from_pay": average("saved_from_pay"),
            "employer": average("employer"),
            "savings_rate": _rate(average),
        },
        "pay_breakdown": pay_breakdown(db, typical, average("take_home")),
        "flow": flow,
        "history_start": starts,
    }
