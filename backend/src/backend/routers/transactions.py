"""The ledger, and the three things about a row that are yours to change."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, Category, Transaction
from ..queries import month_bounds
from ..schemas import TransactionOut, TransactionPage, TransactionPatch

router = APIRouter(tags=["transactions"])


@router.get("/transactions", response_model=TransactionPage)
def list_transactions(
    month: date | None = None,
    category_id: int | None = None,
    account_id: int | None = None,
    search: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session),
) -> TransactionPage:
    stmt = (
        select(Transaction, Category, Account)
        .join(Category, Transaction.category_id == Category.id)
        .join(Account, Transaction.account_id == Account.id)
    )

    if month is not None:
        start, end = month_bounds(month)
        stmt = stmt.where(
            Transaction.occurred_on >= start, Transaction.occurred_on < end
        )
    if category_id is not None:
        stmt = stmt.where(Transaction.category_id == category_id)
    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            or_(
                Transaction.description.ilike(like),
                Transaction.merchant.ilike(like),
                Transaction.notes.ilike(like),
            )
        )

    total = db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )

    stmt = stmt.order_by(
        Transaction.occurred_on.desc(), Transaction.id.desc()
    ).limit(limit).offset(offset)

    rows = [
        TransactionOut(
            id=txn.id,
            occurred_on=txn.occurred_on,
            description=txn.description,
            merchant=txn.merchant,
            amount=txn.amount,
            category_id=category.id,
            category_name=category.name,
            category_kind=category.kind,
            account_id=account.id,
            account_name=account.name,
            notes=txn.notes,
        )
        for txn, category, account in db.execute(stmt).all()
    ]
    return TransactionPage(rows=rows, total=total or 0)


@router.patch("/transactions/{transaction_id}", response_model=TransactionOut)
def update_transaction(
    transaction_id: int,
    body: TransactionPatch,
    db: Session = Depends(get_session),
) -> TransactionOut:
    """Re-file a row, rename its merchant, or annotate it.

    Not the amount, the date or the account — those are the bank's account of
    what happened, and a ledger you can edit to say whatever you like is not a
    ledger. Correcting a guess is a different act from rewriting a fact.
    """
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(404, "no such transaction")

    fields = body.model_dump(exclude_unset=True)
    if "category_id" in fields:
        if db.get(Category, fields["category_id"]) is None:
            raise HTTPException(400, "no such category")
        txn.category_id = fields["category_id"]
    if "merchant" in fields:
        txn.merchant = (fields["merchant"] or "").strip() or None
    if "notes" in fields:
        txn.notes = (fields["notes"] or "").strip() or None

    db.commit()

    category = db.get(Category, txn.category_id)
    account = db.get(Account, txn.account_id)
    return TransactionOut(
        id=txn.id,
        occurred_on=txn.occurred_on,
        description=txn.description,
        merchant=txn.merchant,
        amount=txn.amount,
        category_id=category.id,
        category_name=category.name,
        category_kind=category.kind,
        account_id=account.id,
        account_name=account.name,
        notes=txn.notes,
    )


@router.get("/merchants", response_model=list[str])
def known_merchants(db: Session = Depends(get_session)) -> list[str]:
    """Merchant names already in use, most-used first.

    This is the whole of what used to be three tables, two screens and a merge
    queue. A merchant is a string chosen from the names already on file, so a
    second spelling never arrives and there is nothing to fold back together.
    """
    stmt = (
        select(Transaction.merchant, func.count(Transaction.id).label("n"))
        .where(Transaction.merchant.is_not(None))
        .group_by(Transaction.merchant)
        .order_by(func.count(Transaction.id).desc(), Transaction.merchant)
    )
    return [name for name, _ in db.execute(stmt).all()]
