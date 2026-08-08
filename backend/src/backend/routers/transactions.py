"""Transaction CRUD — the hand-entry half of getting off the spreadsheet."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..merchants import suggest_key
from ..models import (
    Account,
    BudgetPeriod,
    Category,
    Transaction,
    TransactionSource,
)
from ..schemas import TransactionIn, TransactionOut, TransactionPatch

router = APIRouter(prefix="/transactions", tags=["transactions"])


def get_or_create_period(session: Session, year: int, month: int) -> BudgetPeriod:
    period = session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.year == year, BudgetPeriod.month == month
        )
    )
    if period is None:
        period = BudgetPeriod(year=year, month=month)
        session.add(period)
        session.flush()
    return period


def settle_merchant_key(
    session: Session, given: str | None, description: str
) -> str | None:
    """The merchant name to store: what was chosen, or a guess from the text.

    A name typed rather than picked is matched case-insensitively against what
    already exists and snapped to that spelling, so 'harris teeter' joins
    'Harris Teeter' instead of becoming a second entry. This is the whole of
    what the merge queue used to do, done before the row exists.
    """
    name = (given or "").strip() or suggest_key(description)
    return snap_to_existing(session, name) if name else None


def snap_to_existing(session: Session, name: str) -> str:
    """The spelling already in use for this name, or the name unchanged.

    Two rules, both exact — no similarity scoring, because a wrong merge is
    what this whole change exists to make impossible.

    1. Same name, different case: 'harris teeter' is 'Harris Teeter'.
    2. An existing name that is a whole-word *prefix* of the guess. The old
       schema kept a pattern per descriptor, so a hand-merge taught it that
       NETFLIX.COM was Netflix; dropping the patterns lost that, and the guess
       regressed to 'Netflix Com'. The longest matching prefix wins, so
       'Uber Eats Delivery' resolves to 'Uber Eats' and not to 'Uber'.

    Still only a default on an editable field. Rule 2 is a guess, and the
    reason it is allowed to be one is that it lands in a picker the user can
    correct before the row is written.
    """
    exact = session.scalar(
        select(Transaction.merchant_key).where(
            func.lower(Transaction.merchant_key) == name.lower()
        )
    )
    if exact:
        return exact

    prefix = session.scalar(
        select(Transaction.merchant_key)
        .where(Transaction.merchant_key.is_not(None))
        .where(func.lower(name).startswith(func.lower(Transaction.merchant_key) + " "))
        .order_by(func.length(Transaction.merchant_key).desc())
        .limit(1)
    )
    return prefix or name


def to_out(txn: Transaction) -> TransactionOut:
    return TransactionOut(
        id=txn.id,
        occurred_on=txn.occurred_on,
        year=txn.period.year,
        month=txn.period.month,
        raw_description=txn.raw_description,
        merchant_key=txn.merchant_key,
        category_id=txn.category_id,
        category_name=txn.category.name,
        amount=txn.amount,
        is_recurring=txn.is_recurring,
        account_id=txn.account_id,
        account_name=(
            f"{txn.account.institution} {txn.account.name}" if txn.account else None
        ),
        source=txn.source,
    )


def check_account(session: Session, account_id: int | None) -> None:
    """A transaction may only name an account that exists and is still open."""
    if account_id is None:
        return
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(422, f"no account with id {account_id}")
    if account.closed_on is not None:
        raise HTTPException(
            422,
            f"{account.institution} {account.name} was closed on {account.closed_on}",
        )


@router.get("", response_model=list[TransactionOut])
def list_transactions(
    year: int | None = None,
    month: int | None = None,
    category_id: int | None = None,
    account_id: int | None = None,
    q: str | None = Query(default=None, description="substring of the description"),
    limit: int = Query(default=200, le=1000),
    offset: int = 0,
    session: Session = Depends(get_session),
):
    stmt = select(Transaction).join(
        BudgetPeriod, BudgetPeriod.id == Transaction.period_id
    )
    if year is not None:
        stmt = stmt.where(BudgetPeriod.year == year)
    if month is not None:
        stmt = stmt.where(BudgetPeriod.month == month)
    if category_id is not None:
        stmt = stmt.where(Transaction.category_id == category_id)
    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    if q:
        stmt = stmt.where(Transaction.raw_description.ilike(f"%{q}%"))
    stmt = (
        stmt.order_by(
            # Undated rows sort last within their month rather than first.
            Transaction.occurred_on.desc().nullslast(),
            Transaction.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [to_out(t) for t in session.scalars(stmt).all()]


@router.post("", response_model=TransactionOut, status_code=201)
def create_transaction(payload: TransactionIn, session: Session = Depends(get_session)):
    """Create one transaction by hand.

    Deliberately never deduplicates. Two identical charges at the same bar on
    the same night are usually two real rounds, and silently swallowing the
    second would be worse than a duplicate the user can see and delete.
    """
    try:
        year, month = payload.resolved_period()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if session.get(Category, payload.category_id) is None:
        raise HTTPException(422, f"no category with id {payload.category_id}")
    check_account(session, payload.account_id)

    period = get_or_create_period(session, year, month)

    txn = Transaction(
        occurred_on=payload.occurred_on,
        period_id=period.id,
        raw_description=payload.raw_description.strip(),
        merchant_key=settle_merchant_key(
            session, payload.merchant_key, payload.raw_description
        ),
        category_id=payload.category_id,
        amount=payload.amount,
        is_recurring=payload.is_recurring,
        account_id=payload.account_id,
        source=TransactionSource.MANUAL,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return to_out(txn)


@router.patch("/{txn_id}", response_model=TransactionOut)
def update_transaction(
    txn_id: int, payload: TransactionPatch, session: Session = Depends(get_session)
):
    txn = session.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404, "transaction not found")

    data = payload.model_dump(exclude_unset=True)
    if "category_id" in data and session.get(Category, data["category_id"]) is None:
        raise HTTPException(422, f"no category with id {data['category_id']}")
    if "amount" in data and data["amount"] == 0:
        raise HTTPException(422, "amount must not be zero")
    if "account_id" in data:
        check_account(session, data["account_id"])
    if "merchant_key" in data:
        # Blank clears the merchant deliberately — some rows genuinely have no
        # payee ('NON-CHASE ATM WITHDRAW'), and forcing a guess back on would
        # make the field impossible to empty. Anything else snaps to an
        # existing spelling rather than starting a near-duplicate name.
        given = (data["merchant_key"] or "").strip()
        data["merchant_key"] = (
            settle_merchant_key(session, given, "") if given else None
        )

    for key, value in data.items():
        setattr(txn, key, value)

    # Moving the date across a month boundary moves the transaction with it,
    # otherwise it would keep rolling up under the old month.
    if txn.occurred_on is not None:
        txn.period_id = get_or_create_period(
            session, txn.occurred_on.year, txn.occurred_on.month
        ).id

    session.commit()
    session.refresh(txn)
    return to_out(txn)


@router.delete("/{txn_id}", status_code=204)
def delete_transaction(txn_id: int, session: Session = Depends(get_session)):
    txn = session.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404, "transaction not found")
    session.delete(txn)
    session.commit()
