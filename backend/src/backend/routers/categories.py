"""Categories, and what each one is meant to cost."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from ..categorize import UNCATEGORISED, bank_guess, by_description
from ..db import get_session
from ..models import (
    Budget,
    Category,
    MerchantRule,
    PlaidCategoryMap,
    Transaction,
)
from ..queries import budget_plan, regular_list
from ..schemas import (
    BudgetIn,
    BudgetOut,
    BudgetPlan,
    CategoryIn,
    CategoryOut,
    MerchantRuleIn,
    MerchantRuleOut,
    RegularCharge,
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
    db.execute(delete(MerchantRule).where(MerchantRule.category_id == category_id))
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


@router.get("/regular", response_model=list[RegularCharge])
def regular(month: date | None = None, db: Session = Depends(get_session)) -> list:
    """The charges that came in each of the three months before `month` (this
    one by default), and where each is filed."""
    return regular_list(db, month or date.today())  # noqa: DTZ011


@router.put("/merchant-rules", response_model=MerchantRuleOut)
def set_merchant_rule(
    body: MerchantRuleIn, db: Session = Depends(get_session)
) -> MerchantRuleOut:
    """File everything from one merchant under one category, from now on and
    back through its history.

    Moves the rows that are where the bank's label or the old rule put them,
    or unfiled. A row you filed somewhere else by hand stays: that was a
    decision about that one charge. With `category_id` None the rule goes,
    and the rows it moved go back to what the bank's label says.
    """
    rule = db.scalar(select(MerchantRule).where(MerchantRule.merchant == body.merchant))
    old = rule.category_id if rule else None
    if body.category_id is not None and db.get(Category, body.category_id) is None:
        raise HTTPException(404, "no such category")

    if body.category_id is None:
        if rule is not None:
            db.delete(rule)
    elif rule is None:
        db.add(MerchantRule(merchant=body.merchant, category_id=body.category_id))
    else:
        rule.category_id = body.category_id

    unfiled = db.scalar(select(Category.id).where(Category.name == UNCATEGORISED))
    moved = 0
    rows = db.scalars(
        select(Transaction).where(
            or_(
                Transaction.merchant == body.merchant,
                Transaction.merchant.is_(None)
                & (Transaction.description == body.merchant),
            )
        )
    )
    for txn in rows:
        guess = bank_guess(db, txn).id
        if txn.category_id not in {guess, old, unfiled}:
            continue
        # The same order `resolve_category` uses for new rows: a descriptor
        # rule (a transfer) first, then this rule, then the bank's label.
        target = (
            guess if body.category_id is None or by_description(db, txn.description)
            else body.category_id
        )
        if txn.category_id != target:
            txn.category_id = target
            moved += 1
    db.commit()
    return MerchantRuleOut(
        merchant=body.merchant, category_id=body.category_id, moved=moved
    )
