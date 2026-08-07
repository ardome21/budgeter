"""Merchant listing and merging.

Normalization is deliberately conservative, so it under-merges: 'Rhino Market',
'Rhino Mart' and 'Rhino Market Deli' survive as three records for one deli. The
merge endpoint is how a human collapses them in one pass, and the patterns move
with the merge so future imports of any of those spellings resolve correctly.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import queries
from ..db import get_session
from ..models import Merchant, MerchantPattern, Transaction
from ..schemas import MerchantMergeIn, MerchantOut

router = APIRouter(prefix="/merchants", tags=["merchants"])


@router.get("", response_model=list[MerchantOut])
def list_merchants(
    q: str | None = Query(default=None, description="substring of the name"),
    limit: int = Query(default=200, le=1000),
    session: Session = Depends(get_session),
):
    return [
        MerchantOut(
            id=mid,
            canonical_name=name,
            default_category_id=cat,
            transaction_count=count,
        )
        for mid, name, cat, count in queries.merchant_rows(session, q, limit)
    ]


@router.post("/{merchant_id}/merge", response_model=MerchantOut)
def merge_merchant(
    merchant_id: int, payload: MerchantMergeIn, session: Session = Depends(get_session)
):
    """Fold `merchant_id` into `into_id`. The source merchant is removed.

    Transactions and patterns both move, so the merge is permanent: a later
    import of the losing merchant's descriptor resolves to the survivor rather
    than recreating the split.
    """
    source = session.get(Merchant, merchant_id)
    target = session.get(Merchant, payload.into_id)
    if source is None:
        raise HTTPException(404, f"no merchant with id {merchant_id}")
    if target is None:
        raise HTTPException(404, f"no merchant with id {payload.into_id}")
    if source.id == target.id:
        raise HTTPException(422, "cannot merge a merchant into itself")

    session.execute(
        update(Transaction)
        .where(Transaction.merchant_id == source.id)
        .values(merchant_id=target.id)
    )
    session.execute(
        update(MerchantPattern)
        .where(MerchantPattern.merchant_id == source.id)
        .values(merchant_id=target.id)
    )
    session.delete(source)
    session.commit()

    count = session.scalar(
        select(func.count(Transaction.id)).where(Transaction.merchant_id == target.id)
    )
    return MerchantOut(
        id=target.id,
        canonical_name=target.canonical_name,
        default_category_id=target.default_category_id,
        transaction_count=count or 0,
    )
