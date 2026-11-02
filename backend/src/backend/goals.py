"""What the goals need, and when each would be reached.

Every goal draws on one pot: the money you could actually spend on it. That
is cash (after the cards are paid) and the investments that are not
retirement money. Retirement accounts are left out because they cannot be
reached without a penalty, and the HSA because it is for health costs.

The pot is split in goal order, **cash first**: the nearest goal gets the
money that cannot fall in value, and each goal says how much of it is cash
and how much is invested, so a car "covered" by the stock market reads as
exactly that.

What is still needed is then filled from what you put away each month that
stays reachable — what was kept in the bank, plus pay-stub savings lines that
go into a non-retirement account (an ESPP buying into the brokerage). Money
going into a 401(k) is saving, but it is not saving for a house.

No investment return is assumed. It is the conservative choice and the one
with no guess in it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, Goal, PayLineKind
from .overview import ZERO, _latest_balances, overview, stub_for

# How far ahead the projection looks before calling a goal out of reach.
HORIZON_MONTHS = 600

CENT = Decimal("0.01")


def _months_between(start: date, end: date) -> int:
    """Whole months from the first of `start`'s month to `end`'s month."""
    return (end.year - start.year) * 12 + end.month - start.month


def _add_months(month: date, n: int) -> date:
    total = month.year * 12 + month.month - 1 + n
    return date(total // 12, total % 12 + 1, 1)


def _reachable(account: Account) -> bool:
    return not account.is_retirement and (account.subtype or "") != "hsa"


def _pot(db: Session) -> tuple[Decimal, Decimal]:
    """Cash after the cards, and reachable investments, as they stand."""
    cash = invested = ZERO
    for account, amount, _ in _latest_balances(db):
        if account.account_type == "investment":
            if _reachable(account):
                invested += amount
        elif account.group in ("checking", "savings", "credit", "loan"):
            cash += amount
    return cash, invested


def _monthly_saving(db: Session, view: dict) -> dict:
    """What a typical month adds to the pot."""
    kept = view["typical"]["kept"] or ZERO
    from_pay = ZERO
    months = view["typical_months"]
    stub = stub_for(db, months[0]) if months else None
    if stub is not None:
        each = Decimal(stub.periods_per_year) / 12
        ids = [
            line.account_id
            for line in stub.lines
            if line.kind == PayLineKind.SAVING and line.account_id is not None
        ]
        accounts = {
            a.id: a for a in db.scalars(select(Account).where(Account.id.in_(ids)))
        }
        for line in stub.lines:
            account = accounts.get(line.account_id)
            if line.kind == PayLineKind.SAVING and account and _reachable(account):
                from_pay += (line.amount * each).quantize(CENT)
    return {"kept": kept, "from_pay": from_pay, "total": kept + from_pay}


def goal_plan(db: Session, today: date | None = None) -> dict:
    today = today or date.today()  # noqa: DTZ011
    this_month = today.replace(day=1)
    view = overview(db, today=today)
    typical_spent = view["typical"]["spent"] or ZERO
    cash, invested = _pot(db)
    saving = _monthly_saving(db, view)

    goals = list(db.scalars(select(Goal).order_by(Goal.sort_order, Goal.id)))
    rows = []
    cash_left, invested_left = max(cash, ZERO), max(invested, ZERO)
    for goal in goals:
        if goal.months_of_spending is not None:
            target = (goal.months_of_spending * typical_spent).quantize(CENT)
        else:
            target = goal.amount or ZERO
        from_cash = min(cash_left, target)
        cash_left -= from_cash
        from_invested = min(invested_left, target - from_cash)
        invested_left -= from_invested
        rows.append(
            {
                "id": goal.id,
                "name": goal.name,
                "amount": goal.amount,
                "months_of_spending": goal.months_of_spending,
                "target_date": goal.target_date,
                "target": target,
                "covered_cash": from_cash,
                "covered_invested": from_invested,
                "remaining": target - from_cash - from_invested,
                "monthly_needed": None,
                "reached_on": this_month
                if target == from_cash + from_invested
                else None,
                "on_track": True,
            }
        )

    _project(rows, saving["total"], this_month)

    needed_now = sum((r["monthly_needed"] or ZERO for r in rows), ZERO)
    return {
        "cash": cash,
        "invested": invested,
        # What the goals leave untouched, after every one has taken its share.
        "unassigned_cash": cash_left,
        "unassigned_invested": invested_left,
        "monthly_saving": saving["total"],
        "monthly_kept": saving["kept"],
        "monthly_from_pay": saving["from_pay"],
        "typical_spent": typical_spent,
        # Only dated goals ask for a set amount each month; the rest take
        # whatever is left over, so they never make the plan short.
        "monthly_needed": needed_now,
        "short_by": max(ZERO, needed_now - saving["total"]),
        "goals": rows,
    }


def _project(rows: list[dict], monthly: Decimal, start: date) -> None:
    """Fill each goal month by month and record when it is reached.

    Each month's saving goes down the list in order. A goal with a date asks
    for what is left divided by the months to go — recomputed every month, so
    it catches up after a short month — and a goal without one takes whatever
    remains. Earlier goals are never starved for later ones: the order is the
    priority.
    """
    remaining = {r["id"]: r["remaining"] for r in rows}
    for r in rows:
        if r["target_date"] and remaining[r["id"]] > 0:
            months = max(1, _months_between(start, r["target_date"]))
            r["monthly_needed"] = (remaining[r["id"]] / months).quantize(CENT)

    if monthly <= 0:
        for r in rows:
            if remaining[r["id"]] > 0:
                r["on_track"] = r["target_date"] is None
        return

    for n in range(1, HORIZON_MONTHS + 1):
        if all(v <= 0 for v in remaining.values()):
            break
        month = _add_months(start, n)
        available = monthly
        for r in rows:
            left = remaining[r["id"]]
            if left <= 0 or available <= 0:
                continue
            if r["target_date"]:
                months = max(1, _months_between(month, r["target_date"]) + 1)
                want = left / months
            else:
                want = left
            give = min(want, available, left)
            remaining[r["id"]] = left - give
            available -= give
            if remaining[r["id"]] <= CENT:
                remaining[r["id"]] = ZERO
                r["reached_on"] = month

    for r in rows:
        if r["target_date"] and r["remaining"] > 0:
            reached = r["reached_on"]
            r["on_track"] = reached is not None and reached <= r["target_date"]
