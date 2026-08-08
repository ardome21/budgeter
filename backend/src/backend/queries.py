"""Derived views.

Everything the spreadsheet computed in cells lives here as a query instead.
Nothing in this module is stored, so nothing in it can go stale — which is the
failure the workbook kept hitting: a rollup whose SUMIF list stopped matching
the categories actually in use, silently under-reporting for months.
"""

from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .models import (
    BudgetAllocation,
    BudgetPeriod,
    Category,
    CategoryKind,
    FixedCost,
    PaycheckLine,
    PaycheckLineKind,
    Transaction,
)
from .schemas import (
    BiggestPurchase,
    CategoryLine,
    CommitmentSplit,
    FixedCostGroup,
    MonthDays,
    MonthSummary,
    OverviewOut,
    PeriodOut,
)

ZERO = Decimal("0.00")

# The Paycheck sheet is per-paycheck; the Monthly Overview sheet is monthly.
# 8855.92 / 4427.96 is exactly 2, so pay is semi-monthly. Kept as a named
# constant rather than a bare 2 so it is obvious what to change if that ever
# stops being true.
PAYCHECKS_PER_MONTH = 2


def _q(value: Decimal | None) -> Decimal:
    return (value or ZERO).quantize(Decimal("0.01"))


def list_periods(session: Session) -> list[PeriodOut]:
    rows = session.execute(
        select(
            BudgetPeriod.year,
            BudgetPeriod.month,
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.amount), 0),
        )
        .join(Transaction, Transaction.period_id == BudgetPeriod.id, isouter=True)
        .group_by(BudgetPeriod.year, BudgetPeriod.month)
        .order_by(BudgetPeriod.year.desc(), BudgetPeriod.month.desc())
    ).all()
    return [
        PeriodOut(year=y, month=m, transaction_count=n, total=_q(total))
        for y, m, n, total in rows
    ]


def _days(year: int, month: int, today: date) -> MonthDays:
    in_month = monthrange(year, month)[1]
    if (year, month) < (today.year, today.month):
        elapsed = in_month
    elif (year, month) == (today.year, today.month):
        elapsed = today.day
    else:
        elapsed = 0
    return MonthDays(in_month=in_month, elapsed=elapsed, remaining=in_month - elapsed)


def month_summary(
    session: Session, year: int, month: int, today: date | None = None
) -> MonthSummary | None:
    today = today or date.today()
    period = session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.year == year, BudgetPeriod.month == month
        )
    )
    if period is None:
        return None

    spent_by_cat = dict(
        session.execute(
            select(Transaction.category_id, func.sum(Transaction.amount))
            .where(Transaction.period_id == period.id)
            .group_by(Transaction.category_id)
        ).all()
    )
    allocated_by_cat = dict(
        session.execute(
            select(BudgetAllocation.category_id, BudgetAllocation.amount).where(
                BudgetAllocation.period_id == period.id
            )
        ).all()
    )

    categories = session.scalars(
        select(Category).order_by(Category.sort_order, Category.name)
    ).all()

    spent_total = _q(sum(spent_by_cat.values(), ZERO))
    allocated_total = _q(sum(allocated_by_cat.values(), ZERO))

    lines: list[CategoryLine] = []
    for cat in categories:
        spent = _q(spent_by_cat.get(cat.id))
        allocated = _q(allocated_by_cat.get(cat.id))
        if spent == 0 and allocated == 0:
            continue  # a category with no budget and no spending is not news
        lines.append(
            CategoryLine(
                category_id=cat.id,
                category=cat.name,
                kind=cat.kind,
                allocated=allocated,
                spent=spent,
                remaining=_q(allocated - spent),
                pct_used=float(spent / allocated) if allocated else None,
                share_of_spend=float(spent / spent_total) if spent_total else 0.0,
            )
        )

    # Committed vs flexible comes from the transaction, not the category — the
    # gym is a fixed monthly cost that lives in Self Care.
    committed, flexible = session.execute(
        select(
            func.coalesce(
                func.sum(case((Transaction.is_recurring, Transaction.amount), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case((~Transaction.is_recurring, Transaction.amount), else_=0)
                ),
                0,
            ),
        ).where(Transaction.period_id == period.id)
    ).one()
    saved = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.period_id == period.id, Category.kind == CategoryKind.SAVINGS
        )
    )

    biggest = [
        BiggestPurchase(description=d, amount=_q(a), occurred_on=o)
        for d, a, o in session.execute(
            select(
                Transaction.raw_description, Transaction.amount, Transaction.occurred_on
            )
            .where(Transaction.period_id == period.id, Transaction.amount > 0)
            .order_by(Transaction.amount.desc())
            .limit(5)
        ).all()
    ]

    txn_count, undated = session.execute(
        select(
            func.count(Transaction.id),
            func.count(Transaction.id).filter(Transaction.occurred_on.is_(None)),
        ).where(Transaction.period_id == period.id)
    ).one()

    days = _days(year, month, today)
    return MonthSummary(
        year=year,
        month=month,
        categories=lines,
        allocated_total=allocated_total,
        spent_total=spent_total,
        remaining_total=_q(allocated_total - spent_total),
        commitment=CommitmentSplit(
            committed=_q(committed), flexible=_q(flexible), saved=_q(saved)
        ),
        biggest=biggest,
        days=days,
        spent_per_day=_q(spent_total / days.elapsed) if days.elapsed else None,
        safe_per_day=(
            _q((allocated_total - spent_total) / days.remaining)
            if days.remaining and allocated_total
            else None
        ),
        transaction_count=txn_count,
        undated_count=undated,
    )


def overview(session: Session) -> OverviewOut:
    """Monthly Overview, rebuilt from the paycheck and fixed-cost tables."""
    by_kind = dict(
        session.execute(
            select(PaycheckLine.kind, func.sum(PaycheckLine.amount)).group_by(
                PaycheckLine.kind
            )
        ).all()
    )
    per_cheque_gross = _q(by_kind.get(PaycheckLineKind.INCOME))
    insurance = _q(by_kind.get(PaycheckLineKind.INSURANCE))
    savings = _q(by_kind.get(PaycheckLineKind.SAVINGS))
    tax = _q(by_kind.get(PaycheckLineKind.TAX))

    n = PAYCHECKS_PER_MONTH
    gross_monthly = _q(per_cheque_gross * n)
    post_tax = _q((per_cheque_gross - insurance) * n)
    take_home = _q((per_cheque_gross - insurance - savings - tax) * n)

    groups = [
        FixedCostGroup(category=name, amount=_q(amount), lines=lines)
        for name, amount, lines in session.execute(
            select(
                Category.name,
                func.sum(FixedCost.amount),
                func.array_agg(FixedCost.description),
            )
            .join(Category, Category.id == FixedCost.category_id)
            # Top-level rows only. A component is part of its parent's amount —
            # the rent breakdown's thirteen lines sum to the rent charge — so
            # summing both counts that money twice. This query predates
            # components and did exactly that, reporting fixed costs of 3586.27
            # against a real 2032.90 and understating disposable income by the
            # whole rent charge.
            .where(FixedCost.effective_to.is_(None), FixedCost.parent_id.is_(None))
            .group_by(Category.name)
            .order_by(func.sum(FixedCost.amount).desc())
        ).all()
    ]
    fixed_total = _q(sum((g.amount for g in groups), ZERO))

    return OverviewOut(
        gross_monthly=gross_monthly,
        post_tax=post_tax,
        take_home=take_home,
        fixed_costs=fixed_total,
        disposable=_q(take_home - fixed_total),
        auto_saved=_q(savings * n),
        paychecks_per_month=n,
        fixed_by_category=groups,
    )
