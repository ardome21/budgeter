"""Merchant listing, suggestions, merging, and recording what is *not* a match.

Normalization is deliberately conservative, so it under-merges: 'Rhino Market',
'Rhino Mart' and 'Rhino Market Deli' survive as three records for one deli.
This is the review queue for the leftovers — and crucially, saying "no" is
recorded, so the queue can actually be emptied.
"""

from itertools import combinations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import queries
from ..db import get_session
from ..models import Merchant, MerchantPattern, MerchantSplit, Transaction
from ..schemas import (
    MerchantMergeIn,
    MerchantOut,
    RejectIn,
    Suggestion,
    SuggestionMember,
)
from ..suggestions import group_names

router = APIRouter(prefix="/merchants", tags=["merchants"])

MAX_SUGGESTIONS = 25
EXAMPLES_PER_MERCHANT = 4


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


@router.get("/suggestions", response_model=list[Suggestion])
def suggestions(session: Session = Depends(get_session)):
    """Merchants that are probably one place, with the descriptors behind them.

    Only merchants that are actually used are considered — proposing a merge
    of two unused records is busywork.
    """
    rows = session.execute(
        select(
            Merchant.id,
            Merchant.canonical_name,
            func.count(Transaction.id),
        )
        .join(Transaction, Transaction.merchant_id == Merchant.id)
        .group_by(Merchant.id, Merchant.canonical_name)
    ).all()
    if not rows:
        return []

    by_name = {name: (mid, count) for mid, name, count in rows}
    rejected = {
        tuple(sorted((left, right)))
        for left, right in session.execute(
            select(MerchantSplit.left_name, MerchantSplit.right_name)
        ).all()
    }

    groups = group_names(list(by_name), rejected)
    groups.sort(key=lambda g: sum(by_name[n][1] for n in g), reverse=True)

    out: list[Suggestion] = []
    for group in groups[:MAX_SUGGESTIONS]:
        members = []
        for name in sorted(group, key=lambda n: by_name[n][1], reverse=True):
            merchant_id, count = by_name[name]
            examples = session.scalars(
                select(Transaction.raw_description)
                .where(Transaction.merchant_id == merchant_id)
                .distinct()
                .order_by(Transaction.raw_description)
                .limit(EXAMPLES_PER_MERCHANT)
            ).all()
            members.append(
                SuggestionMember(
                    id=merchant_id,
                    canonical_name=name,
                    transaction_count=count,
                    examples=list(examples),
                )
            )
        out.append(
            Suggestion(
                key="|".join(sorted(group)),
                members=members,
                total_transactions=sum(m.transaction_count for m in members),
            )
        )
    return out


@router.post("/suggestions/reject", status_code=204)
def reject(payload: RejectIn, session: Session = Depends(get_session)):
    """Record that these names are different places, so they stop being proposed."""
    existing = {
        tuple(sorted((left, right)))
        for left, right in session.execute(
            select(MerchantSplit.left_name, MerchantSplit.right_name)
        ).all()
    }

    names = sorted(set(payload.names) - {payload.anchor})
    if payload.anchor:
        pairs = [tuple(sorted((payload.anchor, other))) for other in names]
    else:
        pairs = [tuple(sorted(pair)) for pair in combinations(names, 2)]

    for left, right in pairs:
        if (left, right) in existing:
            continue
        session.add(MerchantSplit(left_name=left, right_name=right))
        existing.add((left, right))
    session.commit()


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
