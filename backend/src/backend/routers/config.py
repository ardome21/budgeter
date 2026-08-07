"""Editing the things the workbook kept on its config sheets.

Budget allocations, monthly fixed costs and the paycheck breakdown were all
imported and then unreachable, which meant setting next month's budget still
meant opening Excel.

Fixed costs and paycheck lines carry `effective_from` / `effective_to`, so a
change records what the number became without destroying what it was. A
spreadsheet cell cannot do that, and it is why the workbook could never answer
"what was rent last year".
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import (
    BudgetAllocation,
    BudgetPeriod,
    Category,
    FixedCost,
    PaycheckLine,
    PaycheckLineKind,
)
from ..reconcile import reconcile_month
from ..schemas import Money
from .transactions import get_or_create_period

router = APIRouter(tags=["config"])

CENTS = Decimal("0.01")


# ---------------------------------------------------------------- budgets


class AllocationRow(BaseModel):
    category_id: int
    category: str
    amount: Money


class AllocationsOut(BaseModel):
    year: int
    month: int
    allocations: list[AllocationRow]
    total: Money


class AllocationIn(BaseModel):
    category_id: int
    amount: Decimal


class AllocationsIn(BaseModel):
    """The whole month's budget, replacing whatever was there.

    A full replace rather than per-row edits: a budget is a single decision
    about how one month's money is divided, and applying it in pieces leaves
    the total temporarily wrong.
    """

    allocations: list[AllocationIn]


def _allocations_out(session: Session, period: BudgetPeriod) -> AllocationsOut:
    rows = session.execute(
        select(BudgetAllocation.category_id, Category.name, BudgetAllocation.amount)
        .join(Category, Category.id == BudgetAllocation.category_id)
        .where(BudgetAllocation.period_id == period.id)
        .order_by(Category.sort_order, Category.name)
    ).all()
    return AllocationsOut(
        year=period.year,
        month=period.month,
        allocations=[
            AllocationRow(category_id=cid, category=name, amount=amount)
            for cid, name, amount in rows
        ],
        total=sum((r[2] for r in rows), Decimal("0.00")),
    )


@router.get("/periods/{year}/{month}/allocations", response_model=AllocationsOut)
def get_allocations(year: int, month: int, session: Session = Depends(get_session)):
    period = session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.year == year, BudgetPeriod.month == month
        )
    )
    if period is None:
        return AllocationsOut(
            year=year, month=month, allocations=[], total=Decimal("0.00")
        )
    return _allocations_out(session, period)


@router.put("/periods/{year}/{month}/allocations", response_model=AllocationsOut)
def set_allocations(
    year: int,
    month: int,
    payload: AllocationsIn,
    session: Session = Depends(get_session),
):
    """Replace a month's budget. Zero and negative allocations are dropped."""
    if not 1 <= month <= 12:
        raise HTTPException(422, "month must be between 1 and 12")

    valid = set(session.scalars(select(Category.id)).all())
    unknown = {a.category_id for a in payload.allocations} - valid
    if unknown:
        raise HTTPException(422, f"no category with id {min(unknown)}")

    period = get_or_create_period(session, year, month)
    session.query(BudgetAllocation).filter(
        BudgetAllocation.period_id == period.id
    ).delete()

    seen: set[int] = set()
    for row in payload.allocations:
        amount = row.amount.quantize(CENTS)
        if amount <= 0 or row.category_id in seen:
            continue
        seen.add(row.category_id)
        session.add(
            BudgetAllocation(
                period_id=period.id, category_id=row.category_id, amount=amount
            )
        )
    session.commit()
    return _allocations_out(session, period)


@router.post("/periods/{year}/{month}/allocations/copy", response_model=AllocationsOut)
def copy_allocations(
    year: int,
    month: int,
    from_year: int,
    from_month: int,
    session: Session = Depends(get_session),
):
    """Copy another month's budget over this one.

    Most months are last month with one or two numbers changed; retyping twelve
    categories to alter one is how a budget stops being kept up.
    """
    source = session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.year == from_year, BudgetPeriod.month == from_month
        )
    )
    if source is None:
        raise HTTPException(404, f"no data for {from_year}-{from_month:02d}")

    rows = session.execute(
        select(BudgetAllocation.category_id, BudgetAllocation.amount).where(
            BudgetAllocation.period_id == source.id
        )
    ).all()
    if not rows:
        raise HTTPException(422, f"{from_year}-{from_month:02d} has no budget to copy")

    return set_allocations(
        year,
        month,
        AllocationsIn(
            allocations=[
                AllocationIn(category_id=cid, amount=amount) for cid, amount in rows
            ]
        ),
        session,
    )


# ------------------------------------------------------------ fixed costs


class FixedCostOut(BaseModel):
    id: int
    description: str
    amount: Money
    category_id: int
    category: str
    is_exact: bool
    effective_from: date
    effective_to: date | None
    parent_id: int | None
    merchant_id: int | None
    components: list["FixedCostOut"] = []


class FixedCostIn(BaseModel):
    description: str = Field(min_length=1, max_length=80)
    amount: Decimal
    category_id: int
    is_exact: bool = True
    effective_from: date | None = None
    parent_id: int | None = None


class FixedCostPatch(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=80)
    amount: Decimal | None = None
    category_id: int | None = None
    is_exact: bool | None = None
    effective_to: date | None = None
    # Link the commitment to whoever actually charges for it.
    merchant_id: int | None = None


def _fc_out(fc: FixedCost, children: list[FixedCost] | None = None) -> FixedCostOut:
    return FixedCostOut(
        id=fc.id,
        description=fc.description,
        amount=fc.amount,
        category_id=fc.category_id,
        category=fc.category.name,
        is_exact=fc.is_exact,
        effective_from=fc.effective_from,
        effective_to=fc.effective_to,
        parent_id=fc.parent_id,
        merchant_id=fc.merchant_id,
        components=[_fc_out(c) for c in (children or [])],
    )


@router.get("/fixed-costs", response_model=list[FixedCostOut])
def list_fixed_costs(
    include_ended: bool = False, session: Session = Depends(get_session)
):
    """Current monthly commitments, each with its breakdown if it has one.

    Only top-level rows are returned at the top level: a component is part of
    its parent's amount, and counting both would double the total.
    """
    stmt = select(FixedCost).order_by(FixedCost.amount.desc())
    if not include_ended:
        stmt = stmt.where(FixedCost.effective_to.is_(None))
    rows = session.scalars(stmt).all()

    by_parent: dict[int, list[FixedCost]] = {}
    for fc in rows:
        if fc.parent_id is not None:
            by_parent.setdefault(fc.parent_id, []).append(fc)
    return [
        _fc_out(fc, by_parent.get(fc.id, [])) for fc in rows if fc.parent_id is None
    ]


@router.post("/fixed-costs", response_model=FixedCostOut, status_code=201)
def create_fixed_cost(payload: FixedCostIn, session: Session = Depends(get_session)):
    if session.get(Category, payload.category_id) is None:
        raise HTTPException(422, f"no category with id {payload.category_id}")
    if (
        payload.parent_id is not None
        and session.get(FixedCost, payload.parent_id) is None
    ):
        raise HTTPException(422, f"no fixed cost with id {payload.parent_id}")

    fc = FixedCost(
        description=payload.description.strip(),
        amount=payload.amount.quantize(CENTS),
        category_id=payload.category_id,
        is_exact=payload.is_exact,
        effective_from=payload.effective_from or date.today(),
        parent_id=payload.parent_id,
    )
    session.add(fc)
    session.commit()
    return _fc_out(fc)


@router.patch("/fixed-costs/{cost_id}", response_model=FixedCostOut)
def update_fixed_cost(
    cost_id: int, payload: FixedCostPatch, session: Session = Depends(get_session)
):
    """Change a commitment.

    Changing the *amount* ends the current row and opens a new one from today,
    so last year's rent is still answerable. Everything else edits in place —
    fixing a typo in a description is not a change in what you pay.
    """
    fc = session.get(FixedCost, cost_id)
    if fc is None:
        raise HTTPException(404, "fixed cost not found")

    data = payload.model_dump(exclude_unset=True)
    if "category_id" in data and session.get(Category, data["category_id"]) is None:
        raise HTTPException(422, f"no category with id {data['category_id']}")

    new_amount = data.pop("amount", None)
    for key, value in data.items():
        setattr(fc, key, value.strip() if isinstance(value, str) else value)

    if new_amount is not None:
        amount = Decimal(new_amount).quantize(CENTS)
        if amount != fc.amount:
            today = date.today()
            if fc.effective_from >= today:
                # Still same-day; correct in place rather than leaving a
                # zero-length historical row behind.
                fc.amount = amount
            else:
                fc.effective_to = today
                replacement = FixedCost(
                    description=fc.description,
                    amount=amount,
                    category_id=fc.category_id,
                    is_exact=fc.is_exact,
                    effective_from=today,
                    parent_id=fc.parent_id,
                )
                session.add(replacement)
                session.commit()
                return _fc_out(replacement)

    session.commit()
    return _fc_out(fc)


@router.delete("/fixed-costs/{cost_id}", status_code=204)
def end_fixed_cost(cost_id: int, session: Session = Depends(get_session)):
    """End a commitment as of today rather than erasing it.

    A cancelled subscription still explains last month's spending.
    """
    fc = session.get(FixedCost, cost_id)
    if fc is None:
        raise HTTPException(404, "fixed cost not found")
    for child in session.scalars(
        select(FixedCost).where(FixedCost.parent_id == cost_id)
    ).all():
        child.effective_to = date.today()
    fc.effective_to = date.today()
    session.commit()


# --------------------------------------------------------------- paycheck


class PaycheckLineOut(BaseModel):
    id: int
    description: str
    amount: Money
    kind: PaycheckLineKind
    effective_from: date
    effective_to: date | None


class PaycheckLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=80)
    amount: Decimal
    kind: PaycheckLineKind
    effective_from: date | None = None


class PaycheckLinePatch(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=80)
    amount: Decimal | None = None
    kind: PaycheckLineKind | None = None


@router.get("/paycheck", response_model=list[PaycheckLineOut])
def list_paycheck(include_ended: bool = False, session: Session = Depends(get_session)):
    stmt = select(PaycheckLine).order_by(PaycheckLine.kind, PaycheckLine.amount.desc())
    if not include_ended:
        stmt = stmt.where(PaycheckLine.effective_to.is_(None))
    return session.scalars(stmt).all()


@router.post("/paycheck", response_model=PaycheckLineOut, status_code=201)
def create_paycheck_line(
    payload: PaycheckLineIn, session: Session = Depends(get_session)
):
    line = PaycheckLine(
        description=payload.description.strip(),
        amount=payload.amount.quantize(CENTS),
        kind=payload.kind,
        effective_from=payload.effective_from or date.today(),
    )
    session.add(line)
    session.commit()
    return line


@router.patch("/paycheck/{line_id}", response_model=PaycheckLineOut)
def update_paycheck_line(
    line_id: int, payload: PaycheckLinePatch, session: Session = Depends(get_session)
):
    line = session.get(PaycheckLine, line_id)
    if line is None:
        raise HTTPException(404, "paycheck line not found")
    data = payload.model_dump(exclude_unset=True)
    if "amount" in data and data["amount"] is not None:
        data["amount"] = Decimal(data["amount"]).quantize(CENTS)
    for key, value in data.items():
        setattr(line, key, value.strip() if isinstance(value, str) else value)
    session.commit()
    return line


@router.delete("/paycheck/{line_id}", status_code=204)
def end_paycheck_line(line_id: int, session: Session = Depends(get_session)):
    line = session.get(PaycheckLine, line_id)
    if line is None:
        raise HTTPException(404, "paycheck line not found")
    line.effective_to = date.today()
    session.commit()


# --------------------------------------------------------- reconciliation


class ReconcileRow(BaseModel):
    fixed_cost_id: int
    description: str
    category: str
    expected: Money
    actual: Money | None
    drift: Money | None
    merchant: str | None
    charges: int
    note: str | None
    suggestions: list[tuple[int, str]]


class ReconcileOut(BaseModel):
    year: int
    month: int
    rows: list[ReconcileRow]
    expected_total: Money
    matched_total: Money


@router.get("/periods/{year}/{month}/reconcile", response_model=ReconcileOut)
def reconcile(year: int, month: int, session: Session = Depends(get_session)):
    """What each standing commitment expected, against what actually left.

    The workbook had both numbers on different sheets and no way to compare
    them, which is how rent sat $18 off its expected figure for months.
    """
    matches = reconcile_month(session, year, month)
    rows = [
        ReconcileRow(
            fixed_cost_id=m.fixed_cost_id,
            description=m.description,
            category=m.category,
            expected=m.expected,
            actual=m.actual,
            drift=(m.actual - m.expected) if m.actual is not None else None,
            merchant=m.merchant,
            charges=m.charges,
            note=m.note,
            suggestions=m.suggestions,
        )
        for m in matches
    ]
    return ReconcileOut(
        year=year,
        month=month,
        rows=rows,
        expected_total=sum((r.expected for r in rows), Decimal("0.00")),
        matched_total=sum(
            (r.actual for r in rows if r.actual is not None), Decimal("0.00")
        ),
    )
