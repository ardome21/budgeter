"""CSV import: preview first, commit second.

Nothing is written until the preview comes back and the caller confirms. The
preview is where merchant resolution, category inference and duplicate
detection are shown, because all three are guesses and guesses should be
visible before they become rows.
"""

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..csv_import import CandidateRow, parse_csv, row_hash
from ..db import get_session
from ..merchants import display_name, normalize_merchant
from ..models import (
    Category,
    Merchant,
    MerchantPattern,
    Transaction,
    TransactionSource,
)
from ..schemas import Money
from .transactions import get_or_create_period

router = APIRouter(prefix="/imports", tags=["imports"])


class CategoryOption(BaseModel):
    """A category this merchant has genuinely been used with, and how often."""

    id: int
    name: str
    count: int


class PreviewRow(BaseModel):
    row_number: int
    occurred_on: date | None
    raw_description: str
    amount: Money
    suggested_category_id: int | None
    suggested_category_name: str | None
    # A shop is not one category. Rhino Market & Deli is Food and Drinks on a
    # sandwich run and Groceries on a shop, and both are right — so every
    # category the merchant has actually been filed under is offered, ranked
    # by how often, rather than forcing one answer.
    category_options: list[CategoryOption]
    merchant_name: str | None
    import_hash: str
    duplicate_of: int | None
    notes: list[str]


class PreviewOut(BaseModel):
    rows: list[PreviewRow]
    errors: list[str]
    detected_columns: dict[str, str]
    new_count: int
    duplicate_count: int
    uncategorised_count: int


class CommitRow(BaseModel):
    occurred_on: date | None = None
    year: int | None = Field(default=None, ge=1990, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    raw_description: str = Field(min_length=1, max_length=200)
    amount: Money
    category_id: int
    is_recurring: bool = False
    import_hash: str | None = None


class CommitIn(BaseModel):
    rows: list[CommitRow]


class CommitOut(BaseModel):
    created: int
    skipped_duplicates: int
    errors: list[str]


def _category_history(
    session: Session, merchant_ids: set[int]
) -> dict[int, list[tuple[int, str, int]]]:
    """What each merchant has actually been filed under, most used first.

    Read from the transactions rather than from merchants.default_category_id,
    because that column records whatever the merchant's *first* transaction
    happened to be and does not move when a merge changes the population. On
    this data it disagreed with history for ten merchants, the largest being
    113 transactions. History cannot go stale.
    """
    if not merchant_ids:
        return {}

    rows = session.execute(
        select(
            Transaction.merchant_id,
            Transaction.category_id,
            Category.name,
            func.count(Transaction.id),
        )
        .join(Category, Category.id == Transaction.category_id)
        .where(Transaction.merchant_id.in_(merchant_ids))
        .group_by(Transaction.merchant_id, Transaction.category_id, Category.name)
    ).all()

    out: dict[int, list[tuple[int, str, int]]] = {}
    for merchant_id, category_id, name, count in rows:
        out.setdefault(merchant_id, []).append((category_id, name, count))
    for options in out.values():
        options.sort(key=lambda o: (-o[2], o[1]))
    return out


def _enrich(session: Session, rows: list[CandidateRow]) -> None:
    """Resolve merchants, offer the categories they have been used with, flag
    duplicates."""
    if not rows:
        return

    keys = {r.raw_description: normalize_merchant(r.raw_description) for r in rows}
    known = {
        pattern: (merchant_id, name)
        for pattern, merchant_id, name in session.execute(
            select(
                MerchantPattern.pattern,
                Merchant.id,
                Merchant.canonical_name,
            )
            .join(Merchant, Merchant.id == MerchantPattern.merchant_id)
            .where(MerchantPattern.pattern.in_(set(keys.values()) - {""}))
        ).all()
    }
    hashes = [r.import_hash for r in rows if r.import_hash]
    existing = dict(
        session.execute(
            select(Transaction.import_hash, Transaction.id).where(
                Transaction.import_hash.in_(hashes)
            )
        ).all()
    )

    matched_ids = {merchant_id for merchant_id, _ in known.values()}
    history = _category_history(session, matched_ids)

    for row in rows:
        key = keys[row.raw_description]
        if key and key in known:
            merchant_id, name = known[key]
            row.merchant_id = merchant_id
            row.merchant_name = name
            row.category_options = history.get(merchant_id, [])

            if row.category_options:
                # The most-used category leads; the rest are one click away.
                category_id, category_name, _ = row.category_options[0]
                row.suggested_category_id = category_id
                row.suggested_category_name = category_name
        elif key:
            row.notes.append("new merchant")
        row.duplicate_of = existing.get(row.import_hash)


@router.post("/preview", response_model=PreviewOut)
async def preview(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    flip_sign: bool = Form(default=False),
    session: Session = Depends(get_session),
):
    """Parse a pasted or uploaded CSV. Writes nothing."""
    if file is not None:
        content = (await file.read()).decode("utf-8-sig", errors="replace")
    elif text:
        content = text
    else:
        raise HTTPException(422, "provide either a file or pasted text")

    parsed = parse_csv(content, flip_sign=flip_sign)
    _enrich(session, parsed.rows)

    return PreviewOut(
        rows=[
            PreviewRow(
                row_number=r.row_number,
                occurred_on=r.occurred_on,
                raw_description=r.raw_description,
                amount=r.amount,
                suggested_category_id=r.suggested_category_id,
                suggested_category_name=r.suggested_category_name,
                category_options=[
                    CategoryOption(id=cid, name=name, count=count)
                    for cid, name, count in r.category_options
                ],
                merchant_name=r.merchant_name,
                import_hash=r.import_hash,
                duplicate_of=r.duplicate_of,
                notes=r.notes,
            )
            for r in parsed.rows
        ],
        errors=parsed.errors,
        detected_columns=parsed.detected_columns,
        new_count=sum(1 for r in parsed.rows if r.duplicate_of is None),
        duplicate_count=sum(1 for r in parsed.rows if r.duplicate_of is not None),
        uncategorised_count=sum(
            1 for r in parsed.rows if r.suggested_category_id is None
        ),
    )


@router.post("/commit", response_model=CommitOut)
def commit(payload: CommitIn, session: Session = Depends(get_session)):
    """Write confirmed rows. Rows whose hash already exists are skipped."""
    if not payload.rows:
        raise HTTPException(422, "no rows to commit")

    valid_categories = set(session.scalars(select(Category.id)).all())
    created = 0
    skipped = 0
    errors: list[str] = []

    for index, row in enumerate(payload.rows, start=1):
        if row.category_id not in valid_categories:
            errors.append(f"row {index}: no category with id {row.category_id}")
            continue
        if row.amount == 0:
            errors.append(f"row {index}: amount must not be zero")
            continue

        if row.occurred_on is not None:
            year, month = row.occurred_on.year, row.occurred_on.month
        elif row.year is not None and row.month is not None:
            year, month = row.year, row.month
        else:
            errors.append(f"row {index}: needs occurred_on, or both year and month")
            continue

        digest = row.import_hash or row_hash(
            row.occurred_on, row.raw_description, row.amount
        )
        if session.scalar(
            select(Transaction.id).where(Transaction.import_hash == digest)
        ):
            skipped += 1
            continue

        period = get_or_create_period(session, year, month)
        merchant = _merchant_for(session, row.raw_description)
        session.add(
            Transaction(
                occurred_on=row.occurred_on,
                period_id=period.id,
                raw_description=row.raw_description.strip(),
                merchant_id=merchant.id if merchant else None,
                category_id=row.category_id,
                amount=row.amount,
                is_recurring=row.is_recurring,
                source=TransactionSource.CSV,
                import_hash=digest,
            )
        )
        created += 1

    session.commit()
    return CommitOut(created=created, skipped_duplicates=skipped, errors=errors)


def _merchant_for(session: Session, description: str) -> Merchant | None:
    key = normalize_merchant(description)
    if not key:
        return None
    pattern = session.scalar(
        select(MerchantPattern).where(MerchantPattern.pattern == key)
    )
    if pattern is not None:
        return session.get(Merchant, pattern.merchant_id)
    merchant = Merchant(canonical_name=display_name(key))
    session.add(merchant)
    session.flush()
    session.add(MerchantPattern(merchant_id=merchant.id, pattern=key))
    return merchant
