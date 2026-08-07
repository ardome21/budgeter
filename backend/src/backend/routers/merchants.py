"""Merchant listing, suggestions, merging, and recording what is *not* a match.

Normalization is deliberately conservative, so it under-merges: 'Rhino Market',
'Rhino Mart' and 'Rhino Market Deli' survive as three records for one deli.
This is the review queue for the leftovers — and crucially, saying "no" is
recorded, so the queue can actually be emptied.
"""

from itertools import combinations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .. import queries
from ..db import get_session
from ..models import Merchant, MerchantPattern, MerchantSplit, Transaction
from ..schemas import (
    MerchantMergeIn,
    MerchantOut,
    MerchantRenameIn,
    RejectIn,
    Suggestion,
    SuggestionMember,
)
from ..suggestions import group_names

router = APIRouter(prefix="/merchants", tags=["merchants"])

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


def _examples_for(session: Session, merchant_ids: list[int]) -> dict[int, list[str]]:
    """Descriptors for every merchant in one query.

    One query per merchant is what forced the queue to be capped in the first
    place; a few hundred round trips to render one screen is not a tradeoff
    worth making when a window function does it in one.
    """
    if not merchant_ids:
        return {}

    distinct = (
        select(Transaction.merchant_id, Transaction.raw_description)
        .where(Transaction.merchant_id.in_(merchant_ids))
        .distinct()
        .subquery()
    )
    ranked = select(
        distinct.c.merchant_id,
        distinct.c.raw_description,
        func.row_number()
        .over(
            partition_by=distinct.c.merchant_id,
            order_by=distinct.c.raw_description,
        )
        .label("rank"),
    ).subquery()

    out: dict[int, list[str]] = {}
    for merchant_id, description, _ in session.execute(
        select(ranked).where(ranked.c.rank <= EXAMPLES_PER_MERCHANT)
    ).all():
        out.setdefault(merchant_id, []).append(description)
    return out


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

    # Every remaining group is returned. Capping the list made the queue look
    # static — clearing one only pulled another up from below the cap, so
    # fifteen merges left the header reading "1 of 25" exactly as before, and
    # saved work looked like lost work.
    ids = [by_name[name][0] for group in groups for name in group]
    examples_by_merchant = _examples_for(session, ids)

    out: list[Suggestion] = []
    for group in groups:
        members = []
        for name in sorted(group, key=lambda n: by_name[n][1], reverse=True):
            merchant_id, count = by_name[name]
            members.append(
                SuggestionMember(
                    id=merchant_id,
                    canonical_name=name,
                    transaction_count=count,
                    examples=examples_by_merchant.get(merchant_id, []),
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


@router.patch("/{merchant_id}", response_model=MerchantOut)
def rename_merchant(
    merchant_id: int,
    payload: MerchantRenameIn,
    session: Session = Depends(get_session),
):
    """Give a merchant a name of your choosing.

    The normalized key is a machine's guess — 'Rhino Market Deli' rather than
    'Rhino Market & Deli' — so the display name is worth being able to type.

    merchant_splits is keyed by name, so every recorded "these are different"
    decision is rewritten to follow the rename. Skipping that would quietly
    resurrect proposals the user has already answered.
    """
    merchant = session.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(404, f"no merchant with id {merchant_id}")

    new_name = payload.canonical_name.strip()
    if not new_name:
        raise HTTPException(422, "name must not be blank")
    if new_name == merchant.canonical_name:
        return _merchant_out(session, merchant)

    clash = session.scalar(
        select(Merchant).where(
            Merchant.canonical_name == new_name, Merchant.id != merchant_id
        )
    )
    if clash is not None:
        raise HTTPException(
            409,
            f"'{new_name}' is already used by another merchant — "
            f"merge them instead of naming both the same",
        )

    old_name = merchant.canonical_name
    merchant.canonical_name = new_name
    session.execute(
        update(MerchantSplit)
        .where(MerchantSplit.left_name == old_name)
        .values(left_name=new_name)
    )
    session.execute(
        update(MerchantSplit)
        .where(MerchantSplit.right_name == old_name)
        .values(right_name=new_name)
    )
    session.commit()
    return _merchant_out(session, merchant)


def _merchant_out(session: Session, merchant: Merchant) -> MerchantOut:
    count = session.scalar(
        select(func.count(Transaction.id)).where(Transaction.merchant_id == merchant.id)
    )
    return MerchantOut(
        id=merchant.id,
        canonical_name=merchant.canonical_name,
        default_category_id=merchant.default_category_id,
        transaction_count=count or 0,
    )


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
    # The losing name must not linger in the split records: it no longer
    # refers to anything, and a later merchant created with that name would
    # inherit decisions that were never made about it.
    session.execute(
        delete(MerchantSplit).where(
            (MerchantSplit.left_name == source.canonical_name)
            | (MerchantSplit.right_name == source.canonical_name)
        )
    )
    session.delete(source)
    session.commit()
    return _merchant_out(session, target)
