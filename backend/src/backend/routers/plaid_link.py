"""Linked bank accounts: link an institution, then refresh instead of exporting.

The same rule as the CSV import governs this screen — preview first, commit
second. A bank feed guesses a merchant and a category exactly as a CSV does,
and a guess that writes itself is a guess nobody checks.

What is *not* a guess gets applied immediately and silently: when the bank
rewrites a charge it already sent (a pending coffee posts two days later at a
different amount) or withdraws one entirely, that is the bank correcting its own
record, not a suggestion to review.
"""

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..csv_import import CandidateRow
from ..db import get_session
from ..merchants import suggest_key
from ..models import (
    Account,
    Category,
    PlaidAccount,
    PlaidItem,
    Transaction,
    TransactionSource,
)
from ..plaid_client import (
    ApiException,
    LinkedTransaction,
    PlaidNotConfigured,
    PlaidReauthRequired,
    create_link_token,
    decrypt_token,
    describe_api_error,
    encrypt_token,
    exchange_public_token,
    get_accounts,
    get_institution_name,
    remove_item,
    sync_transactions,
)
from ..schemas import Money
from .imports import (
    CategoryOption,
    NearDuplicate,
    _category_history,
    _find_near_duplicates,
)
from .transactions import get_or_create_period, settle_merchant_key, snap_to_existing

router = APIRouter(prefix="/plaid", tags=["plaid"])


# One person, one laptop. Plaid wants a stable id per end user so that Link can
# recognise a returning one; there is only ever the one here.
LOCAL_USER_ID = "budgeter-local"


def _plaid_error(exc: ApiException) -> str:
    """Plaid's own wording, plus the environment, which is usually the cause.

    The mistakes that land here are configuration ones — a sandbox secret used
    against production, or an account not yet approved for Production access —
    and naming the environment turns the message into the fix.
    """
    return f"Plaid ({settings.plaid_env}) — {describe_api_error(exc)}"


# --------------------------------------------------------------------------
# Wire types
# --------------------------------------------------------------------------


class LinkedAccountOut(BaseModel):
    account_id: int
    name: str
    mask: str | None
    subtype: str | None


class ItemOut(BaseModel):
    id: int
    institution_name: str
    accounts: list[LinkedAccountOut]
    sync_start_on: date
    last_synced_at: datetime | None
    needs_reauth: bool


class StatusOut(BaseModel):
    # False means no credentials in .env. The screen says so plainly rather
    # than offering a button that can only fail.
    configured: bool
    environment: str
    items: list[ItemOut]


class LinkTokenIn(BaseModel):
    # Set to re-authenticate an existing institution rather than add a new one.
    item_id: int | None = None


class LinkTokenOut(BaseModel):
    link_token: str


class ExchangeIn(BaseModel):
    public_token: str
    # Nothing dated before this is ever offered. Defaults to the day after the
    # newest transaction already on file, so a first sync does not re-offer
    # months the workbook already covers.
    sync_start_on: date | None = None


class SyncRow(BaseModel):
    key: str  # Plaid's transaction id — identity through to the commit
    account_id: int
    account_label: str
    occurred_on: date
    raw_description: str
    amount: Money
    suggested_category_id: int | None
    suggested_category_name: str | None
    category_options: list[CategoryOption]
    merchant_key: str | None
    near_duplicates: list[NearDuplicate]
    notes: list[str]


class SyncOut(BaseModel):
    rows: list[SyncRow]
    updated: int  # charges the bank revised — applied already
    removed: int  # charges the bank withdrew — applied already
    near_duplicate_count: int
    uncategorised_count: int
    reauth_needed: list[str]
    errors: list[str]


class CommitRow(BaseModel):
    key: str
    account_id: int
    occurred_on: date
    raw_description: str = Field(min_length=1, max_length=200)
    amount: Money
    category_id: int
    is_recurring: bool = False
    merchant_key: str | None = Field(default=None, max_length=120)


class CommitIn(BaseModel):
    rows: list[CommitRow]


class CommitOut(BaseModel):
    created: int
    skipped: int
    errors: list[str]


# --------------------------------------------------------------------------
# Status and linking
# --------------------------------------------------------------------------


def _item_out(item: PlaidItem) -> ItemOut:
    return ItemOut(
        id=item.id,
        institution_name=item.institution_name,
        accounts=[
            LinkedAccountOut(
                account_id=a.account_id,
                name=a.account.name,
                mask=a.mask,
                subtype=a.subtype,
            )
            for a in item.accounts
        ],
        sync_start_on=item.sync_start_on,
        last_synced_at=item.last_synced_at,
        needs_reauth=item.needs_reauth,
    )


@router.get("/status", response_model=StatusOut)
def status(session: Session = Depends(get_session)):
    items = session.scalars(select(PlaidItem).order_by(PlaidItem.institution_name)).all()
    return StatusOut(
        configured=settings.plaid_configured,
        environment=settings.plaid_env,
        items=[_item_out(i) for i in items],
    )


@router.post("/link-token", response_model=LinkTokenOut)
def link_token(payload: LinkTokenIn, session: Session = Depends(get_session)):
    """Authorise one run of Plaid Link in the browser."""
    access_token = None
    if payload.item_id is not None:
        item = session.get(PlaidItem, payload.item_id)
        if item is None:
            raise HTTPException(404, f"no linked institution with id {payload.item_id}")
        access_token = decrypt_token(item.access_token)

    try:
        return LinkTokenOut(
            link_token=create_link_token(
                user_id=LOCAL_USER_ID, access_token=access_token
            )
        )
    except PlaidNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except ApiException as exc:
        raise HTTPException(502, _plaid_error(exc)) from exc


def _default_start(session: Session) -> date:
    """The day after the newest transaction on file.

    Plaid hands back up to 24 months on a first sync and the workbook already
    holds three years. Without this floor the first refresh is a thousand rows
    to review, every one of them a near-duplicate of history typed by hand.
    """
    newest = session.scalar(select(func.max(Transaction.occurred_on)))
    return date.fromordinal(newest.toordinal() + 1) if newest else date(2000, 1, 1)


@router.post("/exchange", response_model=ItemOut)
def exchange(payload: ExchangeIn, session: Session = Depends(get_session)):
    """Finish linking: swap Link's public token and record the accounts.

    Each account at the bank becomes its own account here. The existing lumped
    `Credit Cards` account and everything filed against it are left untouched —
    the split simply starts on the day the institution was linked.
    """
    try:
        access_token, item_id = exchange_public_token(payload.public_token)
    except PlaidNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except ApiException as exc:
        raise HTTPException(502, _plaid_error(exc)) from exc

    existing = session.scalar(select(PlaidItem).where(PlaidItem.item_id == item_id))
    if existing is not None:
        # Link ran in update mode, or the same bank was added twice. Either way
        # the item is already here; refresh its token and clear the alarm.
        existing.access_token = encrypt_token(access_token)
        existing.needs_reauth = False
        session.commit()
        return _item_out(existing)

    institution_id, institution_name = get_institution_name(access_token)
    item = PlaidItem(
        item_id=item_id,
        institution_id=institution_id,
        institution_name=institution_name[:60],
        access_token=encrypt_token(access_token),
        sync_start_on=payload.sync_start_on or _default_start(session),
        needs_reauth=False,
        linked_at=datetime.now(UTC),
    )
    session.add(item)
    session.flush()

    for remote in get_accounts(access_token):
        name = (remote.official_name or remote.name)[:60]
        account = session.scalar(
            select(Account).where(
                Account.institution == institution_name[:60], Account.name == name
            )
        )
        if account is None:
            account = Account(
                institution=institution_name[:60],
                name=name,
                is_retirement=(remote.subtype or "").lower()
                in {"401k", "ira", "roth", "roth 401k", "403b", "pension"},
            )
            session.add(account)
            session.flush()
        session.add(
            PlaidAccount(
                item_id=item.id,
                plaid_account_id=remote.plaid_account_id,
                account_id=account.id,
                mask=remote.mask,
                subtype=remote.subtype,
            )
        )

    session.commit()
    session.refresh(item)
    return _item_out(item)


@router.delete("/items/{item_id}")
def unlink(item_id: int, session: Session = Depends(get_session)):
    """Stop syncing an institution. Its accounts and transactions stay.

    Only the mapping goes. Transactions already committed are as real as any
    other row, and the accounts still hold balance history that net worth is
    built from.
    """
    item = session.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(404, f"no linked institution with id {item_id}")

    remove_item(decrypt_token(item.access_token))
    session.execute(delete(PlaidAccount).where(PlaidAccount.item_id == item.id))
    session.delete(item)
    session.commit()
    return {"unlinked": item.institution_name}


@router.post("/items/{item_id}/rewind", response_model=ItemOut)
def rewind(item_id: int, session: Session = Depends(get_session)):
    """Offer everything since `sync_start_on` again on the next refresh.

    The escape hatch for a row unticked by mistake. Committing advances the
    cursor past every row in the batch, including the ones left unticked —
    that is what makes unticking mean "no, not this one". Clearing the cursor
    replays the lot, and rows already committed are filtered out by their
    transaction id, so only the genuinely missing ones come back.
    """
    item = session.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(404, f"no linked institution with id {item_id}")
    item.cursor = None
    session.commit()
    session.refresh(item)
    return _item_out(item)


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


def _apply_revisions(
    session: Session, modified: list[LinkedTransaction], removed: list[str]
) -> tuple[int, int]:
    """Push the bank's own corrections through to rows already committed.

    These are not reviewed. A pending charge that posts at a different amount
    is the bank settling its record, and holding that behind a confirmation
    prompt would leave the ledger knowingly wrong until someone clicked.
    """
    updated = 0
    for txn in modified:
        row = session.scalar(
            select(Transaction).where(
                Transaction.source_ref == txn.transaction_id,
                Transaction.source == TransactionSource.LINKED,
            )
        )
        if row is None:
            continue  # never committed — it is still waiting in a preview
        period = get_or_create_period(session, txn.occurred_on.year, txn.occurred_on.month)
        row.occurred_on = txn.occurred_on
        row.period_id = period.id
        row.raw_description = txn.name[:200]
        row.amount = txn.amount
        updated += 1

    dropped = 0
    if removed:
        result = session.execute(
            delete(Transaction).where(
                Transaction.source_ref.in_(removed),
                Transaction.source == TransactionSource.LINKED,
            )
        )
        dropped = result.rowcount or 0
    return updated, dropped


def _suggest(
    session: Session, pairs: list[tuple[CandidateRow, LinkedTransaction]]
) -> None:
    """Fill in a merchant and the categories it has been filed under.

    Plaid's own merchant name leads, because it is cleaner than anything the
    normalizer can recover from a raw descriptor — but it is still snapped to
    the spelling already in use, so `Netflix` from Plaid joins the `Netflix`
    with three years of history behind it instead of starting a second one.

    Plaid's category is deliberately *not* mapped to a category id. Its taxonomy
    is not the user's, and a wrong category that arrives pre-selected is the one
    nobody re-reads. It goes in the notes, where it informs without deciding.
    """
    guesses: dict[str, str | None] = {}
    for row, txn in pairs:
        source = txn.merchant_name or txn.name
        if source in guesses:
            continue
        guess = (txn.merchant_name or "").strip() or suggest_key(txn.name)
        guesses[source] = snap_to_existing(session, guess) if guess else None

    history = _category_history(session, {g for g in guesses.values() if g})

    for row, txn in pairs:
        guess = guesses[txn.merchant_name or txn.name]
        if not guess:
            continue
        row.merchant_key = guess
        row.category_options = history.get(guess.lower(), [])
        if row.category_options:
            category_id, category_name, _ = row.category_options[0]
            row.suggested_category_id = category_id
            row.suggested_category_name = category_name
        else:
            row.notes.append("new merchant")
            if txn.category:
                row.notes.append(f"Plaid calls this {txn.category}")


@router.post("/sync", response_model=SyncOut)
def sync(session: Session = Depends(get_session)):
    """Pull every linked institution and return what needs reviewing.

    The cursor is **not** advanced here. Only a commit moves it, so closing the
    preview without acting re-offers exactly the same rows next time rather
    than stepping past them for good.
    """
    if not settings.plaid_configured:
        raise HTTPException(503, "Plaid is not configured — see .env.example")

    items = session.scalars(select(PlaidItem)).all()
    if not items:
        raise HTTPException(422, "no institutions are linked yet")

    account_by_plaid_id = {
        pa.plaid_account_id: pa
        for pa in session.scalars(select(PlaidAccount)).all()
    }

    pairs: list[tuple[CandidateRow, LinkedTransaction]] = []
    labels: dict[str, str] = {}
    updated = removed = 0
    reauth_needed: list[str] = []
    errors: list[str] = []

    for item in items:
        if item.needs_reauth:
            reauth_needed.append(item.institution_name)
            continue
        try:
            diff = sync_transactions(decrypt_token(item.access_token), item.cursor)
        except PlaidReauthRequired:
            item.needs_reauth = True
            reauth_needed.append(item.institution_name)
            continue
        except Exception as exc:  # noqa: BLE001 — one bad bank must not stop the rest
            errors.append(f"{item.institution_name}: {exc}")
            continue

        item_updated, item_removed = _apply_revisions(
            session, diff.modified, diff.removed
        )
        updated += item_updated
        removed += item_removed

        # Already committed: the cursor did not advance last time, or a rewind
        # replayed the batch. Resolved before deciding whether this item has
        # anything to review, so a replay of nothing but known rows still
        # advances the cursor instead of repeating forever.
        already = set(
            session.scalars(
                select(Transaction.source_ref).where(
                    Transaction.source_ref.in_([t.transaction_id for t in diff.added]),
                    Transaction.source == TransactionSource.LINKED,
                )
            ).all()
        )

        offered = 0
        for txn in diff.added:
            if txn.transaction_id in already:
                continue
            # A pending charge is provisional — the bank will withdraw it and
            # re-send it once it posts, usually at a different amount. Waiting
            # for the posted version costs a day and saves reviewing each one
            # twice.
            if txn.pending:
                continue
            if txn.occurred_on < item.sync_start_on:
                continue
            mapping = account_by_plaid_id.get(txn.plaid_account_id)
            if mapping is None:
                continue  # an account added at the bank since linking
            row = CandidateRow(
                row_number=len(pairs) + 1,
                occurred_on=txn.occurred_on,
                raw_description=txn.name[:200],
                amount=txn.amount,
            )
            pairs.append((row, txn))
            offered += 1
            labels[txn.transaction_id] = (
                f"{item.institution_name} {mapping.account.name}"
                + (f" ••{mapping.mask}" if mapping.mask else "")
            )

        if offered:
            # Held until the commit, so the cursor advances to exactly the
            # point this batch ended — and stays put if the review is
            # abandoned, which is what re-offers these rows next time.
            item.pending_cursor = diff.cursor
        else:
            # Nothing to review: revisions, pending charges, or rows below the
            # start date. There is no commit coming, so advance now rather than
            # re-fetching the same empty diff on every refresh forever.
            item.cursor = diff.cursor
            item.pending_cursor = None

        item.last_synced_at = datetime.now(UTC)

    _suggest(session, pairs)
    _find_near_duplicates(session, [r for r, _ in pairs])
    session.commit()

    account_ids = {
        t.transaction_id: account_by_plaid_id[t.plaid_account_id].account_id
        for _, t in pairs
    }
    return SyncOut(
        rows=[
            SyncRow(
                key=txn.transaction_id,
                account_id=account_ids[txn.transaction_id],
                account_label=labels[txn.transaction_id],
                occurred_on=row.occurred_on,
                raw_description=row.raw_description,
                amount=row.amount,
                suggested_category_id=row.suggested_category_id,
                suggested_category_name=row.suggested_category_name,
                category_options=[
                    CategoryOption(id=cid, name=name, count=count)
                    for cid, name, count in row.category_options
                ],
                merchant_key=row.merchant_key,
                near_duplicates=[
                    NearDuplicate(
                        id=m.id,
                        occurred_on=m.occurred_on,
                        raw_description=m.raw_description,
                        amount=m.amount,
                        days_apart=m.days_apart,
                    )
                    for m in row.near_duplicates
                ],
                notes=row.notes,
            )
            for row, txn in pairs
        ],
        updated=updated,
        removed=removed,
        near_duplicate_count=sum(1 for r, _ in pairs if r.near_duplicates),
        uncategorised_count=sum(1 for r, _ in pairs if r.suggested_category_id is None),
        reauth_needed=reauth_needed,
        errors=errors,
    )


@router.post("/commit", response_model=CommitOut)
def commit(payload: CommitIn, session: Session = Depends(get_session)):
    """Write the reviewed rows, then advance every cursor.

    Cursors move last and only here. An unticked row is a decision that it does
    not belong, so the cursor steps past it too — `rewind` is there for when
    that decision was a mistake.
    """
    if not payload.rows:
        raise HTTPException(422, "no rows to commit")

    valid_categories = set(session.scalars(select(Category.id)).all())
    valid_accounts = set(session.scalars(select(Account.id)).all())
    on_file = set(
        session.scalars(
            select(Transaction.source_ref).where(
                Transaction.source_ref.in_([r.key for r in payload.rows]),
                Transaction.source == TransactionSource.LINKED,
            )
        ).all()
    )

    created = 0
    skipped = 0
    errors: list[str] = []
    written: set[str] = set()

    for index, row in enumerate(payload.rows, start=1):
        if row.category_id not in valid_categories:
            errors.append(f"row {index}: no category with id {row.category_id}")
            continue
        if row.account_id not in valid_accounts:
            errors.append(f"row {index}: no account with id {row.account_id}")
            continue
        if row.key in on_file or row.key in written:
            skipped += 1
            continue
        written.add(row.key)

        period = get_or_create_period(
            session, row.occurred_on.year, row.occurred_on.month
        )
        merchant_key = (
            settle_merchant_key(session, row.merchant_key, row.raw_description)
            if row.merchant_key is None or row.merchant_key.strip()
            else None
        )
        session.add(
            Transaction(
                occurred_on=row.occurred_on,
                period_id=period.id,
                raw_description=row.raw_description.strip(),
                merchant_key=merchant_key,
                category_id=row.category_id,
                amount=row.amount,
                is_recurring=row.is_recurring,
                account_id=row.account_id,
                source=TransactionSource.LINKED,
                # Identity, not a content hash: the bank rewrites a charge when
                # it posts, and this is what recognises it as the same one.
                source_ref=row.key,
            )
        )
        created += 1

    # Every item that had a batch out for review now moves on. This is the only
    # place a cursor advances, which is what makes abandoning a preview safe.
    for item in session.scalars(
        select(PlaidItem).where(PlaidItem.pending_cursor.is_not(None))
    ).all():
        item.cursor = item.pending_cursor
        item.pending_cursor = None

    session.commit()
    return CommitOut(created=created, skipped=skipped, errors=errors)
