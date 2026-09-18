"""Accounts and their latest balance."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, Balance
from ..schemas import AccountOut, AccountPatch

router = APIRouter(tags=["accounts"])


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(
    include_closed: bool = False, db: Session = Depends(get_session)
) -> list[AccountOut]:
    """Every account, each carrying its most recent reading.

    The balance comes from a correlated subquery on the newest `as_of` for that
    account, not from the newest row overall. Accounts are read on different
    days — a brokerage posts overnight, a card posts immediately — and taking
    the global maximum would leave every account that did not report today
    showing nothing at all.
    """
    newest = (
        select(func.max(Balance.as_of))
        .where(Balance.account_id == Account.id)
        .correlate(Account)
        .scalar_subquery()
    )

    stmt = (
        select(Account, Balance.amount, Balance.as_of)
        .outerjoin(
            Balance,
            (Balance.account_id == Account.id) & (Balance.as_of == newest),
        )
        .order_by(Account.institution, Account.name)
    )
    if not include_closed:
        stmt = stmt.where(Account.closed_on.is_(None))

    return [
        AccountOut.model_validate(account).model_copy(
            update={"balance": amount, "balance_as_of": as_of}
        )
        for account, amount, as_of in db.execute(stmt).all()
    ]


@router.patch("/accounts/{account_id}", response_model=AccountOut)
def update_account(
    account_id: int, body: AccountPatch, db: Session = Depends(get_session)
) -> Account:
    """The two things about an account that Plaid cannot tell you.

    Whether it is retirement money — yours, but not reachable — and whether it
    is closed. Everything else here is the institution's to report.
    """
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "no such account")

    fields = body.model_dump(exclude_unset=True)
    for name, value in fields.items():
        setattr(account, name, value)
    db.commit()
    return account
