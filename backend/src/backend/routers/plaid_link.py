"""Linking a bank, and pulling what it has.

One Refresh asks every bank for balances, holdings and transactions and writes
what comes back. There is no review step — see `refresh` for why it went. What
was kept was paid for: a linked row is keyed by Plaid's transaction id and
never by a hash of its contents, revisions and removals apply as the bank
reports them, pending charges are not written at all, and nothing a refresh
does overwrites a category you chose by hand.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import plaid_client
from ..categorize import resolve_category
from ..config import settings
from ..db import get_session
from ..merchants import resolve
from ..models import (
    Account,
    Balance,
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
from ..schemas import LinkedItemOut, RefreshOut

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


@router.post("/refresh", response_model=RefreshOut)
def refresh(db: Session = Depends(get_session)) -> RefreshOut:
    """Every bank: balances, holdings and transactions, written as they arrive.

    There is no review. The previous version previewed each bank's new rows
    and advanced the cursor only on commit, and the result was four of six
    banks with a preview parked and not one of their transactions in the
    ledger. Nobody reviews; a review step that is never taken loses data. The
    category comes from Plaid's label through the map, and a row filed wrongly
    is fixed on the Transactions screen — once, and a re-sync never undoes it.

    One bank failing does not stop the others: each is rolled back and named in
    `failures`, because a single 502 for the lot hides *which* bank is the
    problem and makes a stale login look like a broken app.
    """
    _require_plaid()
    return refresh_everything(db)


def refresh_everything(db: Session) -> RefreshOut:
    """The body of `refresh`, callable from a script."""
    totals = dict.fromkeys(
        ("added", "modified", "removed", "accounts_updated", "holdings_updated"), 0
    )
    failures: list[str] = []
    for item in db.scalars(select(PlaidItem).order_by(PlaidItem.institution_name)):
        try:
            one = _sync_item(db, item)
        except HTTPException as exc:
            # Rolled back so one bank's half-applied sync cannot ride along
            # with the next one's commit.
            db.rollback()
            failures.append(f"{item.institution_name}: {exc.detail}")
            continue
        for key in totals:
            totals[key] += one[key]
    return RefreshOut(failures=failures, **totals)


def _sync_item(db: Session, item: PlaidItem) -> dict[str, int]:
    """One bank, start to finish, committed. Raises HTTPException to fail it."""
    token = plaid_client.decrypt_token(item.access_token)
    counts = {"added": 0, "modified": 0, "removed": 0, "holdings_updated": 0}

    # Cleared first and set again by whichever call finds the login stale, so
    # a bank that was signed back in to stops being reported as needing it.
    item.needs_reauth = False
    counts["accounts_updated"] = _refresh_accounts(db, item)
    if item.needs_reauth:
        db.commit()
        raise HTTPException(409, "needs to be signed in to again")
    if "investments" in item.product_list:
        counts["holdings_updated"] = _refresh_holdings(db, item, token)

    # An item answers for the products it was created with and no others.
    # Asking a brokerage linked for investments alone to sync transactions
    # returns ADDITIONAL_CONSENT_REQUIRED, which looks like the refresh being
    # broken rather than like a question it was never granted leave to answer.
    if "transactions" in item.product_list:
        try:
            diff = plaid_client.sync_transactions(token, item.cursor)
        except PlaidReauthRequired:
            item.needs_reauth = True
            db.commit()
            raise HTTPException(409, "needs to be signed in to again") from None
        except PlaidNotConfigured as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, describe_api_error(exc)) from exc

        homeless: list[str] = []
        counts["removed"] = _apply_removals(db, diff.removed)
        counts["modified"] = _apply_revisions(db, item, diff.modified, homeless)
        counts["added"] = _write_new(db, item, diff.added, homeless)
        # A row with no account on this side holds the cursor, so the next
        # refresh asks for it again — by which time `_refresh_accounts` may well
        # have created the account. Advancing would consume a real charge that
        # was never written, with nothing on screen to say so. Re-asking is
        # free: rows already written are skipped by transaction id.
        if not homeless:
            item.cursor = diff.cursor

    item.last_synced_at = datetime.now(UTC)
    db.commit()
    return counts


def _write_new(
    db: Session, item: PlaidItem, incoming: list, homeless: list[str]
) -> int:
    """Write the rows this database does not hold yet. Returns the count.

    Pending charges are skipped: Plaid withdraws a pending row and sends a new
    one, under a new id, when it posts — usually at a different amount and with
    a rewritten descriptor. Writing both would count the coffee twice.

    A row is keyed by Plaid's transaction id, so a rewind or a repeated page
    can never write it twice.
    """
    by_plaid_id = {
        account.plaid_account_id: account
        for account in db.scalars(select(Account).where(Account.item_id == item.id))
    }
    already = set(
        db.scalars(
            select(Transaction.plaid_transaction_id).where(
                Transaction.plaid_transaction_id.in_(
                    [t.transaction_id for t in incoming] or [""]
                )
            )
        )
    )

    written = 0
    for txn in incoming:
        if txn.pending or txn.transaction_id in already:
            continue
        if txn.occurred_on < item.sync_start_on:
            continue
        account = by_plaid_id.get(txn.plaid_account_id)
        if account is None:
            homeless.append(txn.transaction_id)
            continue

        merchant = resolve(db, txn.name, txn.merchant_name)
        db.add(
            Transaction(
                plaid_transaction_id=txn.transaction_id,
                account_id=account.id,
                category_id=resolve_category(
                    db, txn.pfc_detailed, txn.pfc_primary, txn.name
                ).id,
                occurred_on=txn.occurred_on,
                amount=txn.amount,
                description=txn.name,
                merchant=merchant,
                plaid_category_primary=txn.pfc_primary,
                plaid_category_detailed=txn.pfc_detailed,
                created_at=datetime.now(UTC),
            )
        )
        # Flushed per row so the next row's merchant resolution can see it.
        # Sessions run with autoflush off, so without this a batch of ten Uber
        # Eats charges would each resolve against the database as it was before
        # any of them existed.
        db.flush()
        already.add(txn.transaction_id)
        written += 1
    return written


@router.delete("/items/{item_id}", status_code=204)
def unlink(item_id: int, db: Session = Depends(get_session)) -> None:
    """Stop syncing an institution. Its accounts and history stay."""
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(404, "no such linked bank")

    # Already gone at Plaid's end is fine — the row here is what matters.
    with contextlib.suppress(Exception):
        plaid_client.remove_item(plaid_client.decrypt_token(item.access_token))

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
            # Re-adopted, not just updated. `unlink` nulls `item_id` on the way
            # out, and every sync builds its account map from
            # `Account.item_id == item.id` — so an account that came back
            # without this stayed orphaned for good and its transactions were
            # dropped by `_write_new` for having nowhere to go.
            account.item_id = item.id
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

    # Sessions run with autoflush off, and the caller's very next act is a
    # `select(Account).where(Account.item_id == item.id)`. Without this the
    # re-adoption above is still pending, that query cannot see it, and the
    # account reads as unknown for one more refresh.
    db.flush()
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


def _apply_revisions(
    db: Session, item: PlaidItem, revised: list, homeless: list[str]
) -> int:
    """The bank correcting its own record. Applied without asking.

    Only the fields the bank owns — date, amount, descriptor, and its labels.
    The category and the merchant are the user's answer to the row, and a
    revision must not quietly discard a correction they made by hand.

    A revision of a row this database never wrote is written as new.
    """
    touched = 0
    unknown = []
    for txn in revised:
        existing = db.scalar(
            select(Transaction).where(
                Transaction.plaid_transaction_id == txn.transaction_id
            )
        )
        if existing is None:
            unknown.append(txn)
            continue
        existing.occurred_on = txn.occurred_on
        existing.amount = txn.amount
        existing.description = txn.name
        existing.plaid_category_primary = txn.pfc_primary
        existing.plaid_category_detailed = txn.pfc_detailed
        touched += 1
    return touched + _write_new(db, item, unknown, homeless)
