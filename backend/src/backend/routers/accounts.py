"""Accounts and net worth.

The workbook stored balances wide — a new column per snapshot date, which is
why there were only eight in two years. As rows, snapshots are unbounded and
net worth over time is one query.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, AccountBalance
from ..schemas import (
    AccountIn,
    AccountOut,
    AccountPatch,
    BalanceIn,
    BalanceOut,
    NetWorthOut,
    NetWorthPoint,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _to_out(session: Session, account: Account) -> AccountOut:
    rows = session.execute(
        select(AccountBalance.as_of, AccountBalance.balance)
        .where(AccountBalance.account_id == account.id)
        .order_by(AccountBalance.as_of.desc())
        .limit(2)
    ).all()
    latest = rows[0] if rows else None
    previous = rows[1] if len(rows) > 1 else None
    return AccountOut(
        id=account.id,
        institution=account.institution,
        name=account.name,
        is_retirement=account.is_retirement,
        latest_balance=latest[1] if latest else None,
        latest_as_of=latest[0] if latest else None,
        change=(latest[1] - previous[1]) if latest and previous else None,
        snapshot_count=session.scalar(
            select(func.count(AccountBalance.id)).where(
                AccountBalance.account_id == account.id
            )
        )
        or 0,
    )


@router.get("", response_model=list[AccountOut])
def list_accounts(session: Session = Depends(get_session)):
    """Every account with its latest balance and the move since the one before."""
    accounts = session.scalars(
        select(Account).order_by(
            Account.is_retirement.desc(), Account.institution, Account.name
        )
    ).all()
    return [_to_out(session, a) for a in accounts]


@router.post("", response_model=AccountOut, status_code=201)
def create_account(payload: AccountIn, session: Session = Depends(get_session)):
    clash = session.scalar(
        select(Account).where(
            Account.institution == payload.institution.strip(),
            Account.name == payload.name.strip(),
        )
    )
    if clash is not None:
        raise HTTPException(
            409, f"{payload.institution} / {payload.name} already exists"
        )
    account = Account(
        institution=payload.institution.strip(),
        name=payload.name.strip(),
        is_retirement=payload.is_retirement,
    )
    session.add(account)
    session.commit()
    return _to_out(session, account)


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(
    account_id: int, payload: AccountPatch, session: Session = Depends(get_session)
):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")

    data = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    for key in ("institution", "name"):
        if key in data:
            data[key] = data[key].strip()
    merged = {
        "institution": data.get("institution", account.institution),
        "name": data.get("name", account.name),
    }
    clash = session.scalar(
        select(Account).where(
            Account.institution == merged["institution"],
            Account.name == merged["name"],
            Account.id != account_id,
        )
    )
    if clash is not None:
        raise HTTPException(
            409, f"{merged['institution']} / {merged['name']} already exists"
        )

    for key, value in data.items():
        setattr(account, key, value)
    session.commit()
    return _to_out(session, account)


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: int, session: Session = Depends(get_session)):
    """Removes the account and its snapshots. Net worth history goes with it."""
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    session.delete(account)
    session.commit()


@router.get("/{account_id}/balances", response_model=list[BalanceOut])
def list_balances(account_id: int, session: Session = Depends(get_session)):
    if session.get(Account, account_id) is None:
        raise HTTPException(404, "account not found")
    return session.scalars(
        select(AccountBalance)
        .where(AccountBalance.account_id == account_id)
        .order_by(AccountBalance.as_of.desc())
    ).all()


@router.put("/{account_id}/balances", response_model=BalanceOut)
def record_balance(
    account_id: int, payload: BalanceIn, session: Session = Depends(get_session)
):
    """Record a balance for a date, replacing any existing one.

    PUT rather than POST because a snapshot is identified by its date: taking
    two readings of the same account on the same day means the second is a
    correction, not a second holding.
    """
    if session.get(Account, account_id) is None:
        raise HTTPException(404, "account not found")

    amount = payload.balance.quantize(Decimal("0.01"))
    existing = session.scalar(
        select(AccountBalance).where(
            AccountBalance.account_id == account_id,
            AccountBalance.as_of == payload.as_of,
        )
    )
    if existing is not None:
        existing.balance = amount
        session.commit()
        return existing

    snapshot = AccountBalance(
        account_id=account_id, as_of=payload.as_of, balance=amount
    )
    session.add(snapshot)
    session.commit()
    return snapshot


@router.delete("/balances/{balance_id}", status_code=204)
def delete_balance(balance_id: int, session: Session = Depends(get_session)):
    snapshot = session.get(AccountBalance, balance_id)
    if snapshot is None:
        raise HTTPException(404, "snapshot not found")
    session.delete(snapshot)
    session.commit()


@router.get("/net-worth", response_model=NetWorthOut)
def net_worth(session: Session = Depends(get_session)):
    """Net worth on every date anything was recorded.

    Liabilities count. The workbook's own Total row omitted the student loan
    and the credit card balance, which flattered March 2024 by $6,000 and
    October by $309.89.
    """
    rows = session.execute(
        select(
            AccountBalance.as_of,
            func.sum(AccountBalance.balance),
            func.coalesce(
                func.sum(AccountBalance.balance).filter(Account.is_retirement), 0
            ),
            func.coalesce(
                func.sum(AccountBalance.balance).filter(~Account.is_retirement), 0
            ),
            func.count(AccountBalance.id),
        )
        .join(Account, Account.id == AccountBalance.account_id)
        .group_by(AccountBalance.as_of)
        .order_by(AccountBalance.as_of)
    ).all()

    return NetWorthOut(
        points=[
            NetWorthPoint(
                as_of=as_of,
                net_worth=total,
                retirement=retirement,
                liquid=liquid,
                accounts_reported=count,
            )
            for as_of, total, retirement, liquid, count in rows
        ],
        accounts_tracked=session.scalar(select(func.count(Account.id))) or 0,
    )
