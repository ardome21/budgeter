"""The ledger, and the three things about a row that are yours to change."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..categorize import UNCATEGORISED, resolve_category
from ..db import get_session
from ..models import Account, Category, Transaction
from ..queries import month_bounds
from ..schemas import (
    RefileOut,
    TransactionOut,
    TransactionPage,
    TransactionPatch,
)

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
            pending=txn.pending,
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
    also_filed = 0
    if "category_id" in fields:
        if db.get(Category, fields["category_id"]) is None:
            raise HTTPException(400, "no such category")
        txn.category_id = fields["category_id"]
        if body.apply_to_merchant and txn.merchant:
            also_filed = _file_merchant(
                db, txn.merchant, fields["category_id"], besides=txn.id
            )
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
        pending=txn.pending,
        also_filed=also_filed,
    )


def _file_merchant(
    db: Session, merchant: str, category_id: int, besides: int
) -> int:
    """Apply one filing decision to the rest of that merchant's unfiled rows.

    `besides` is the row being edited, excluded so the count is how many
    *others* moved. Excluded by id rather than by relying on it having been
    flushed already: sessions here run with autoflush off, so the pending
    change to that row is invisible to this statement and it would otherwise
    count itself.

    Only rows still sitting in `Uncategorised` move. A row you filed by hand is
    a decision, and a bulk action must never overwrite one — you might well
    have put a single Amazon charge under Travel on purpose. That is also what
    makes this safe to press twice.

    Nothing is stored about the merchant itself, and a future refresh of the
    same shop still files by Plaid's label — see `categorize`.
    """
    result = db.execute(
        update(Transaction)
        .where(Transaction.id != besides)
        .where(Transaction.merchant == merchant)
        .where(Transaction.category_id != category_id)
        .where(
            Transaction.category_id.in_(
                select(Category.id).where(Category.name == UNCATEGORISED)
            )
        )
        .values(category_id=category_id)
    )
    return result.rowcount or 0


@router.post("/transactions/refile-uncategorised", response_model=RefileOut)
def refile_uncategorised(db: Session = Depends(get_session)) -> RefileOut:
    """Run every unfiled row back through the resolver.

    For rows that arrived before their Plaid label was mapped: add the mapping
    in Settings, press this, and they follow. Rows nothing can place are left
    where they are rather than guessed at.
    """
    unfiled = db.scalars(
        select(Transaction)
        .join(Category, Transaction.category_id == Category.id)
        .where(Category.name == UNCATEGORISED)
        .order_by(Transaction.occurred_on)
    ).all()

    moved = 0
    for txn in unfiled:
        category = resolve_category(
            db,
            txn.plaid_category_detailed,
            txn.plaid_category_primary,
            txn.description,
        )
        if category.id == txn.category_id:
            continue
        txn.category_id = category.id
        moved += 1

    db.commit()
    return RefileOut(moved=moved, left=len(unfiled) - moved)


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
