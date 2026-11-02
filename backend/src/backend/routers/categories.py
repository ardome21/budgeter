"""Categories, and what each one is meant to cost."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Budget, Category, PlaidCategoryMap, Transaction
from ..queries import budget_plan
from ..schemas import (
    BudgetIn,
    BudgetOut,
    BudgetPlan,
    CategoryIn,
    CategoryOut,
)

router = APIRouter(tags=["categories"])


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(db: Session = Depends(get_session)) -> list[CategoryOut]:
    """Each with how many rows are filed under it — which says whether it can
    be deleted before anyone tries."""
    counts = dict(
        db.execute(
            select(Transaction.category_id, func.count(Transaction.id)).group_by(
                Transaction.category_id
            )
        ).all()
    )
    return [
        CategoryOut.model_validate(c).model_copy(
            update={"transaction_count": counts.get(c.id, 0)}
        )
        for c in db.scalars(
            select(Category).order_by(Category.sort_order, Category.name)
        )
    ]


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryIn, db: Session = Depends(get_session)) -> Category:
    clash = select(Category).where(func.lower(Category.name) == body.name.lower())
    if db.scalar(clash):
        raise HTTPException(409, f"{body.name} already exists")
    category = Category(name=body.name, kind=body.kind, sort_order=body.sort_order)
    db.add(category)
    db.commit()
    return category


@router.patch("/categories/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: int, body: CategoryIn, db: Session = Depends(get_session)
) -> Category:
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "no such category")
    clash = db.scalar(
        select(Category)
        .where(func.lower(Category.name) == body.name.lower())
        .where(Category.id != category_id)
    )
    if clash is not None:
        # Checked rather than left to the unique constraint, which surfaces as
        # a 500 and a stack trace where create already answers 409 and says
        # what is in the way.
        raise HTTPException(409, f"{body.name} already exists")
    category.name = body.name
    category.kind = body.kind
    category.sort_order = body.sort_order
    db.commit()
    return category


@router.delete("/categories/{category_id}", status_code=204)
def delete_category(category_id: int, db: Session = Depends(get_session)) -> None:
    """Refused while anything is filed here.

    Deleting a category that has transactions would either orphan them or
    silently re-file them somewhere. Both lose information the person deleting
    it did not intend to lose — so the answer is to say what is in the way.
    """
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "no such category")

    in_use = db.scalar(
        select(func.count(Transaction.id)).where(Transaction.category_id == category_id)
    )
    if in_use:
        raise HTTPException(
            409, f"{in_use} transactions are filed under {category.name}"
        )

    db.execute(delete(Budget).where(Budget.category_id == category_id))
    # Plaid labels mapped here stop being mapped, and rows carrying them fall
    # to Uncategorised. Left in place, the map's foreign key refused the
    # delete with a bare 500: Loan Payments, with no rows, could not go.
    db.execute(
        delete(PlaidCategoryMap).where(PlaidCategoryMap.category_id == category_id)
    )
    db.delete(category)
    db.commit()


@router.get("/budgets/{year}/{month}/plan", response_model=BudgetPlan)
def plan(year: int, month: int, db: Session = Depends(get_session)) -> dict:
    """The budget in force for a month, set beside what last month cost and
    what each category typically costs, with a proposal from each.

    A proposal, not a save: the screen fills its boxes from this and nothing
    is written until Save.
    """
    return budget_plan(db, date(year, month, 1))


@router.put("/budgets/{year}/{month}", response_model=list[BudgetOut])
def set_budgets(
    year: int,
    month: int,
    body: list[BudgetIn],
    db: Session = Depends(get_session),
) -> list[Budget]:
    """Replace this month's budgets wholesale.

    A whole-month PUT rather than per-category PATCH because that is what the
    screen does: the budget for a month is edited as one table and saved once.
    An amount of zero is a deliberate "nothing here"; to remove a target
    entirely, leave the category out.

    The set then holds for every later month that has none of its own (see
    `budget_source`). An empty list removes this month's own set, so the month
    goes back to whatever was carried into it.
    """
    when = date(year, month, 1)
    db.execute(delete(Budget).where(Budget.month == when))
    for row in body:
        db.add(Budget(category_id=row.category_id, month=when, amount=row.amount))
    db.commit()
    return list(db.scalars(select(Budget).where(Budget.month == when)))
