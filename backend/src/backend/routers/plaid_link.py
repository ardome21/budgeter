"""Linking a bank, and pulling what it has.

The shape of this is the part worth keeping from the previous version, because
every rule in it was paid for. Preview first and commit second; the cursor
advances only on commit; a linked row is keyed by Plaid's transaction id and
never by a hash of its contents; revisions and removals apply silently; pending
charges are not offered at all.

What changed is what it writes into: eleven tables became four, and holdings
arrive priced by the institution instead of being reconstructed from a CSV.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import plaid_client
from ..config import settings
from ..db import get_session
from ..merchants import resolve
from ..models import (
    Account,
    Balance,
    Category,
    CategoryKind,
    Holding,
    PlaidItem,
    Security,
    SecurityKind,
    Transaction,
)
from ..plaid_client import (
    PlaidNotConfigured,
    PlaidReauthRequired,
    describe_api_error,
)
from ..schemas import CommitIn, LinkedItemOut, PreviewOut, PreviewRow

router = APIRouter(prefix="/linked", tags=["linked"])

# Plaid's security types, mapped onto ours. Anything unrecognised is OTHER
# rather than a guess — a security whose kind is wrong is better than one whose
# kind is confidently wrong.
SECURITY_KINDS = {
    "equity": SecurityKind.EQUITY,
    "etf": SecurityKind.ETF,
    "mutual fund": SecurityKind.MUTUAL_FUND,
    "fixed income": SecurityKind.FIXED_INCOME,
    "cash": SecurityKind.CASH,
}


def _require_plaid() -> None:
    if not settings.plaid_configured:
        raise HTTPException(
            503,
            "Plaid is not configured — set PLAID_CLIENT_ID, PLAID_SECRET and "
            "PLAID_TOKEN_KEY in .env",
        )


class LinkTokenIn(BaseModel):
    # Which products to create the item with. This is the one decision about an
    # item that cannot be revisited: adding `investments` later means running
    # Link again in update mode with fresh consent, and a brokerage linked
    # without it returns balances and no holdings, which looks like an empty
    # portfolio rather than a mislink.
    products: list[str] = ["transactions"]
    item_id: int | None = None


class ExchangeIn(BaseModel):
    public_token: str
    products: list[str] = ["transactions"]


@router.get("/items", response_model=list[LinkedItemOut])
def list_items(db: Session = Depends(get_session)) -> list[LinkedItemOut]:
    counts = dict(
        db.execute(
            select(Account.item_id, func.count(Account.id)).group_by(Account.item_id)
        ).all()
    )
    return [
        LinkedItemOut(
            id=item.id,
            institution_name=item.institution_name,
            products=item.products,
            needs_reauth=item.needs_reauth,
            sync_start_on=item.sync_start_on,
            last_synced_at=(
                item.last_synced_at.isoformat() if item.last_synced_at else None
            ),
            account_count=counts.get(item.id, 0),
        )
        for item in db.scalars(select(PlaidItem).order_by(PlaidItem.institution_name))
    ]


@router.post("/link-token")
def link_token(body: LinkTokenIn, db: Session = Depends(get_session)) -> dict[str, str]:
    _require_plaid()

    access_token = None
    if body.item_id is not None:
        item = db.get(PlaidItem, body.item_id)
        if item is None:
            raise HTTPException(404, "no such linked bank")
        access_token = plaid_client.decrypt_token(item.access_token)

    try:
        token = plaid_client.create_link_token(
            user_id="budgeter",
            access_token=access_token,
            products=body.products,
        )
    except Exception as exc:
        raise HTTPException(502, describe_api_error(exc)) from exc
    return {"link_token": token}


@router.post("/exchange", response_model=LinkedItemOut)
def exchange(body: ExchangeIn, db: Session = Depends(get_session)) -> LinkedItemOut:
    """Turn Link's one-shot public token into a durable item."""
    _require_plaid()

    try:
        access_token, item_id = plaid_client.exchange_public_token(body.public_token)
        institution_id, name = plaid_client.get_institution_name(access_token)
    except Exception as exc:
        raise HTTPException(502, describe_api_error(exc)) from exc

    existing = db.scalar(select(PlaidItem).where(PlaidItem.item_id == item_id))
    if existing is not None:
        raise HTTPException(409, f"{existing.institution_name} is already linked")

    item = PlaidItem(
        item_id=item_id,
        institution_id=institution_id,
        institution_name=name,
        access_token=plaid_client.encrypt_token(access_token),
        products=",".join(body.products),
        # A new item starts with no floor and no cursor: take everything the
        # institution will give, which is what an empty database wants.
        sync_start_on=date(2000, 1, 1),
        linked_at=datetime.now(UTC),
    )
    db.add(item)
    db.commit()

    _refresh_accounts(db, item)
    db.commit()

    return LinkedItemOut(
        id=item.id,
        institution_name=item.institution_name,
        products=item.products,
        needs_reauth=item.needs_reauth,
        sync_start_on=item.sync_start_on,
        last_synced_at=None,
        account_count=len(item.accounts),
    )


@router.post("/items/{item_id}/preview", response_model=PreviewOut)
def preview(item_id: int, db: Session = Depends(get_session)) -> PreviewOut:
    """Everything the bank has that this database does not.

    Balances, holdings, revisions and removals are applied here and now —
    they are the institution correcting its own record, not a suggestion, and
    holding them behind a prompt leaves the ledger knowingly wrong until
    somebody clicks. Only *additions* are offered for review, because only an
    addition carries a guess (a category, a merchant) that can be wrong.
    """
    _require_plaid()
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(404, "no such linked bank")

    token = plaid_client.decrypt_token(item.access_token)

    accounts_updated = _refresh_accounts(db, item)
    holdings_updated = 0
    if "investments" in item.product_list:
        holdings_updated = _refresh_holdings(db, item, token)

    try:
        diff = plaid_client.sync_transactions(token, item.cursor)
    except PlaidReauthRequired:
        item.needs_reauth = True
        db.commit()
        raise HTTPException(
            409, f"{item.institution_name} needs to be signed in to again"
        ) from None
    except PlaidNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, describe_api_error(exc)) from exc

    item.needs_reauth = False
    item.last_synced_at = datetime.now(UTC)

    by_plaid_id = {
        account.plaid_account_id: account
        for account in db.scalars(select(Account).where(Account.item_id == item.id))
    }

    removed = _apply_removals(db, diff.removed)
    modified = _apply_revisions(db, diff.modified)

    already = set(
        db.scalars(
            select(Transaction.plaid_transaction_id).where(
                Transaction.plaid_transaction_id.in_(
                    [t.transaction_id for t in diff.added] or [""]
                )
            )
        )
    )

    rows: list[PreviewRow] = []
    for txn in diff.added:
        # Pending charges get withdrawn and re-sent when they post, with a new
        # date, often a new amount and usually a rewritten descriptor. Offering
        # them means reviewing the same coffee twice.
        if txn.pending:
            continue
        if txn.transaction_id in already:
            continue
        if txn.occurred_on < item.sync_start_on:
            continue
        account = by_plaid_id.get(txn.plaid_account_id)
        if account is None:
            continue

        merchant = resolve(db, txn.name, txn.merchant_name)
        suggested = _suggest_category(db, merchant)
        rows.append(
            PreviewRow(
                transaction_id=txn.transaction_id,
                occurred_on=txn.occurred_on,
                description=txn.name,
                merchant=merchant,
                amount=txn.amount,
                account_id=account.id,
                account_name=account.name,
                category_id=suggested.id if suggested else None,
                category_name=suggested.name if suggested else None,
                # Plaid's own taxonomy, shown but never mapped to a category
                # id. It is not the user's taxonomy and never will be.
                plaid_category=txn.category,
            )
        )

    rows.sort(key=lambda r: (r.occurred_on, r.description))

    # A sync that offers nothing reviewable advances immediately. Without this,
    # a rewind that re-offers nothing repeats forever.
    if rows:
        item.pending_cursor = diff.cursor
        cursor_advanced = False
    else:
        item.cursor = diff.cursor
        item.pending_cursor = None
        cursor_advanced = True

    db.commit()

    return PreviewOut(
        rows=rows,
        modified=modified,
        removed=removed,
        accounts_updated=accounts_updated,
        holdings_updated=holdings_updated,
        cursor_advanced=cursor_advanced,
        message=None if rows else "Nothing new to review.",
    )


@router.post("/commit", response_model=PreviewOut)
def commit(body: CommitIn, db: Session = Depends(get_session)) -> PreviewOut:
    """Write the ticked rows, and advance the cursor past all of them.

    An unticked row is a rejection, and the cursor passes it too — that is what
    makes "re-offer everything" the escape hatch rather than the norm. The
    cursor moves here and nowhere else, so abandoning a preview costs nothing.
    """
    item = db.get(PlaidItem, body.item_id)
    if item is None:
        raise HTTPException(404, "no such linked bank")
    if item.pending_cursor is None:
        raise HTTPException(409, "nothing is waiting to be committed")

    accept = set(body.accept)
    token = plaid_client.decrypt_token(item.access_token)

    try:
        diff = plaid_client.sync_transactions(token, item.cursor)
    except Exception as exc:
        raise HTTPException(502, describe_api_error(exc)) from exc

    by_plaid_id = {
        account.plaid_account_id: account
        for account in db.scalars(select(Account).where(Account.item_id == item.id))
    }
    existing = set(
        db.scalars(select(Transaction.plaid_transaction_id).where(
            Transaction.plaid_transaction_id.is_not(None)
        ))
    )

    written = 0
    for txn in diff.added:
        if txn.transaction_id not in accept or txn.transaction_id in existing:
            continue
        account = by_plaid_id.get(txn.plaid_account_id)
        if account is None:
            continue
        merchant = resolve(db, txn.name, txn.merchant_name)
        category = _suggest_category(db, merchant) or _fallback_category(db)
        db.add(
            Transaction(
                plaid_transaction_id=txn.transaction_id,
                account_id=account.id,
                category_id=category.id,
                occurred_on=txn.occurred_on,
                amount=txn.amount,
                description=txn.name,
                merchant=merchant,
                created_at=datetime.now(UTC),
            )
        )
        written += 1

    item.cursor = item.pending_cursor
    item.pending_cursor = None
    item.last_synced_at = datetime.now(UTC)
    db.commit()

    return PreviewOut(
        rows=[],
        modified=written,
        removed=0,
        accounts_updated=0,
        holdings_updated=0,
        cursor_advanced=True,
        message=f"{written} added.",
    )


@router.post("/items/{item_id}/rewind", status_code=204)
def rewind(item_id: int, db: Session = Depends(get_session)) -> None:
    """Clear the cursor so the bank re-offers everything.

    The escape hatch for an untick that was a mistake. Rows already committed
    are filtered out by transaction id on the way back, so only the genuinely
    missing ones return.
    """
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(404, "no such linked bank")
    item.cursor = None
    item.pending_cursor = None
    db.commit()


@router.delete("/items/{item_id}", status_code=204)
def unlink(item_id: int, db: Session = Depends(get_session)) -> None:
    """Stop syncing an institution. Its accounts and history stay."""
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(404, "no such linked bank")

    try:
        plaid_client.remove_item(plaid_client.decrypt_token(item.access_token))
    except Exception:  # noqa: BLE001, S110 — already gone at Plaid's end is fine
        pass

    db.execute(
        Account.__table__.update()
        .where(Account.item_id == item.id)
        .values(item_id=None)
    )
    db.delete(item)
    db.commit()


# --- the parts that write ---------------------------------------------------


def _refresh_accounts(db: Session, item: PlaidItem) -> int:
    """Create or update this item's accounts, and record today's balance.

    The balance snapshot is dated by the day it was read, so refreshing twice
    in one day overwrites rather than accumulates — two readings on the same
    date are the same fact measured twice, not two facts.
    """
    token = plaid_client.decrypt_token(item.access_token)
    try:
        accounts = plaid_client.get_accounts(token)
    except PlaidReauthRequired:
        item.needs_reauth = True
        return 0

    # The local day, deliberately. A balance snapshot means "what this
    # account was worth today" in the timezone the person refreshing is in;
    # dating an 8pm refresh by UTC would file it under tomorrow.
    today = date.today()  # noqa: DTZ011
    touched = 0
    for incoming in accounts:
        account = db.scalar(
            select(Account).where(
                Account.plaid_account_id == incoming.plaid_account_id
            )
        )
        if account is None:
            account = Account(
                plaid_account_id=incoming.plaid_account_id,
                item_id=item.id,
                name=incoming.official_name or incoming.name,
                institution=item.institution_name,
                mask=incoming.mask,
                account_type=incoming.type,
                subtype=incoming.subtype,
                is_liability=incoming.is_liability,
                is_retirement=_looks_like_retirement(incoming),
            )
            db.add(account)
            db.flush()
        else:
            account.mask = incoming.mask
            account.account_type = incoming.type
            account.subtype = incoming.subtype
            account.is_liability = incoming.is_liability

        balance = incoming.signed_balance
        if balance is None:
            continue

        existing = db.scalar(
            select(Balance).where(
                Balance.account_id == account.id, Balance.as_of == today
            )
        )
        if existing is None:
            db.add(Balance(account_id=account.id, as_of=today, amount=balance))
        else:
            existing.amount = balance
        touched += 1

    return touched


def _looks_like_retirement(incoming) -> bool:
    """Retirement money is yours but not reachable, and Plaid says so in the
    subtype: 401k, ira, roth, 403b."""
    subtype = (incoming.subtype or "").lower()
    return any(marker in subtype for marker in ("401", "403", "ira", "roth", "457"))


def _refresh_holdings(db: Session, item: PlaidItem, token: str) -> int:
    """Record what is held, priced by the institution that holds it."""
    try:
        snapshot = plaid_client.get_holdings(token)
    except PlaidReauthRequired:
        item.needs_reauth = True
        return 0
    except Exception:  # noqa: BLE001
        # An item linked without the investments product answers this with an
        # error. That is a fact about the link, not a failure of the refresh,
        # and it must not take the transaction sync down with it.
        return 0

    securities: dict[str, Security] = {}
    for incoming in snapshot.securities:
        security = db.scalar(
            select(Security).where(
                Security.plaid_security_id == incoming.security_id
            )
        )
        if security is None:
            security = Security(
                plaid_security_id=incoming.security_id,
                name=incoming.name,
                ticker=incoming.ticker,
                cusip=incoming.cusip,
                kind=SECURITY_KINDS.get(incoming.type.lower(), SecurityKind.OTHER),
                is_cash_equivalent=incoming.is_cash_equivalent,
            )
            db.add(security)
            db.flush()
        securities[incoming.security_id] = security

    by_plaid_id = {
        account.plaid_account_id: account
        for account in db.scalars(select(Account).where(Account.item_id == item.id))
    }

    written = 0
    for position in snapshot.holdings:
        account = by_plaid_id.get(position.plaid_account_id)
        security = securities.get(position.security_id)
        if account is None or security is None:
            continue

        # Plaid dates the price it quotes. Where it does not, the snapshot is
        # dated today — the day it was read — rather than left undated.
        as_of = position.as_of or date.today()  # noqa: DTZ011 — local day, as above

        existing = db.scalar(
            select(Holding).where(
                Holding.account_id == account.id,
                Holding.security_id == security.id,
                Holding.as_of == as_of,
            )
        )
        if existing is None:
            db.add(
                Holding(
                    account_id=account.id,
                    security_id=security.id,
                    as_of=as_of,
                    quantity=position.quantity,
                    price=position.price,
                    value=position.value,
                    cost_basis=position.cost_basis,
                )
            )
        else:
            existing.quantity = position.quantity
            existing.price = position.price
            existing.value = position.value
            existing.cost_basis = position.cost_basis
        written += 1

    return written


def _apply_removals(db: Session, transaction_ids: list[str]) -> int:
    if not transaction_ids:
        return 0
    result = db.execute(
        delete(Transaction).where(
            Transaction.plaid_transaction_id.in_(transaction_ids)
        )
    )
    return result.rowcount or 0


def _apply_revisions(db: Session, revised: list) -> int:
    """The bank correcting its own record. Applied without asking.

    Only the fields the bank owns — date, amount, descriptor. The category and
    the merchant are the user's answer to the row, and a revision must not
    quietly discard a correction they made by hand.
    """
    touched = 0
    for txn in revised:
        existing = db.scalar(
            select(Transaction).where(
                Transaction.plaid_transaction_id == txn.transaction_id
            )
        )
        if existing is None:
            continue
        existing.occurred_on = txn.occurred_on
        existing.amount = txn.amount
        existing.description = txn.name
        touched += 1
    return touched


def _suggest_category(db: Session, merchant: str | None) -> Category | None:
    """Where this merchant was filed last time.

    A merchant has a history of categories, not a default — there is nowhere to
    store one and nothing that would keep it current. The most recent answer is
    the best guess available, and it is still only a guess on an editable row.
    """
    if not merchant:
        return None
    category_id = db.scalar(
        select(Transaction.category_id)
        .where(Transaction.merchant == merchant)
        .order_by(Transaction.occurred_on.desc())
        .limit(1)
    )
    return db.get(Category, category_id) if category_id else None


def _fallback_category(db: Session) -> Category:
    """Somewhere to put a charge nothing suggests a home for.

    Created on demand rather than seeded, so it only exists once something has
    actually needed it.
    """
    category = db.scalar(select(Category).where(Category.name == "Uncategorised"))
    if category is None:
        category = Category(
            name="Uncategorised", kind=CategoryKind.SPENDING, sort_order=999
        )
        db.add(category)
        db.flush()
    return category
