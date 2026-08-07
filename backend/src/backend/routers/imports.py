"""CSV import: preview first, commit second.

Nothing is written until the preview comes back and the caller confirms. The
preview is where merchant resolution, category inference and duplicate
detection are shown, because all three are guesses and guesses should be
visible before they become rows.
"""

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
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


class PreviewRow(BaseModel):
    row_number: int
    occurred_on: date | None
    raw_description: str
    amount: Money
    suggested_category_id: int | None
    suggested_category_name: str | None
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


def _enrich(session: Session, rows: list[CandidateRow]) -> None:
    """Resolve merchants, infer categories from history, flag duplicates.

    Category inference reads the merchant's default, which the workbook import
    already populated from three years of hand-categorised history — so most
    rows arrive already correct.
    """
    if not rows:
        return

    keys = {r.raw_description: normalize_merchant(r.raw_description) for r in rows}
    known = {
        pattern: (merchant_id, name, default_category)
        for pattern, merchant_id, name, default_category in session.execute(
            select(
                MerchantPattern.pattern,
                Merchant.id,
                Merchant.canonical_name,
                Merchant.default_category_id,
            )
            .join(Merchant, Merchant.id == MerchantPattern.merchant_id)
            .where(MerchantPattern.pattern.in_(set(keys.values()) - {""}))
        ).all()
    }
    category_names = dict(session.execute(select(Category.id, Category.name)).all())

    hashes = [r.import_hash for r in rows if r.import_hash]
    existing = dict(
        session.execute(
            select(Transaction.import_hash, Transaction.id).where(
                Transaction.import_hash.in_(hashes)
            )
        ).all()
    )

    for row in rows:
        key = keys[row.raw_description]
        if key and key in known:
            merchant_id, name, default_category = known[key]
            row.merchant_id = merchant_id
            row.merchant_name = name
            if default_category:
                row.suggested_category_id = default_category
                row.suggested_category_name = category_names.get(default_category)
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
        merchant = _merchant_for(session, row.raw_description, row.category_id)
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


def _merchant_for(
    session: Session, description: str, category_id: int
) -> Merchant | None:
    key = normalize_merchant(description)
    if not key:
        return None
    pattern = session.scalar(
        select(MerchantPattern).where(MerchantPattern.pattern == key)
    )
    if pattern is not None:
        return session.get(Merchant, pattern.merchant_id)
    merchant = Merchant(
        canonical_name=display_name(key), default_category_id=category_id
    )
    session.add(merchant)
    session.flush()
    session.add(MerchantPattern(merchant_id=merchant.id, pattern=key))
    return merchant
