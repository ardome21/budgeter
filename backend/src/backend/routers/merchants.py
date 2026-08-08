"""The merchant names already in use, for the entry and import pickers.

All that is left of what used to be three tables, two screens, a merge
endpoint, a suggestion engine and a record of rejected pairs. Those existed to
reconcile spellings *after* they diverged. Offering the existing names at the
moment of entry stops them diverging, which needs one query.

Ranked by use, not alphabetically: 278 of 403 names are used exactly once, so
an alphabetical picker buries the handful anyone actually types.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Transaction

router = APIRouter(prefix="/merchants", tags=["merchants"])


class MerchantKey(BaseModel):
    key: str
    count: int


@router.get("/keys", response_model=list[MerchantKey])
def list_keys(
    q: str | None = Query(default=None, description="substring of the name"),
    limit: int = Query(default=20, le=200),
    session: Session = Depends(get_session),
):
    """Merchant names in use, most-used first."""
    stmt = (
        select(Transaction.merchant_key, func.count(Transaction.id).label("n"))
        .where(Transaction.merchant_key.is_not(None))
        .group_by(Transaction.merchant_key)
        .order_by(func.count(Transaction.id).desc(), Transaction.merchant_key)
        .limit(limit)
    )
    if q:
        stmt = stmt.where(Transaction.merchant_key.ilike(f"%{q}%"))
    return [MerchantKey(key=key, count=n) for key, n in session.execute(stmt).all()]
