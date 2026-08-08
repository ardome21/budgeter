"""Matching fixed costs against what actually left the account.

The workbook could not do this. Its fixed-cost sheet said rent was 1553.37 and
its spending sheet said BILT CARD HOUSING charged 1535.17, and nothing
connected the two — so an $18 drift sat there for months, in a cell, unread.

Matching reuses the merchant normalizer rather than inventing a second one: a
fixed cost called "Netflix" normalizes to the same key as the descriptor
"NETFLIX.COM", which is exactly the resolution the import already performs.
Where that fails there is no guessing — the cost is reported as unmatched,
which is itself worth knowing.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .merchants import normalize_merchant
from .models import (
    BudgetPeriod,
    FixedCost,
    Transaction,
)

ZERO = Decimal("0.00")


@dataclass
class Match:
    fixed_cost_id: int
    description: str
    category: str
    expected: Decimal
    actual: Decimal | None
    merchant: str | None
    charges: int
    note: str | None
    suggestions: list[str]


def _key_for_description(session: Session, key: str) -> str | None:
    """Resolve a commitment's description to a merchant name in use.

    Matches the normalized description against the normalized merchant name, so
    'Netflix' finds 'Netflix' however the descriptor behind it was spelled.
    Where the bill and the charge are named differently — rent, billed as
    BILT CARD HOUSING — nothing matches, and `fixed_costs.merchant_key` is what
    carries the answer instead.
    """
    row = session.execute(
        select(Transaction.merchant_key)
        .where(Transaction.merchant_key.is_not(None))
        .where(func.lower(Transaction.merchant_key) == key)
        .limit(1)
    ).first()
    return row[0] if row else None


def suggest_merchants(session: Session, description: str, limit: int = 3) -> list[str]:
    """Plausible merchant names for an unmatched commitment, by shared words.

    A suggestion, never an assignment. 'The Observer' should offer
    'Charlotte Observer' rather than quietly adopting it.
    """
    # Words only, with \y word boundaries. Without them 'inner' matches inside
    # 'dinner' and 'one' inside 'phone', so Inner Peaks proposed "Reggie Repay
    # For Dinner" — the kind of suggestion that teaches you to ignore the list.
    words = [w for w in normalize_merchant(description).split() if len(w) > 2]
    if not words:
        return []
    pattern = r"\y(" + "|".join(words) + r")\y"
    rows = session.execute(
        select(Transaction.merchant_key)
        .where(Transaction.merchant_key.is_not(None))
        .where(func.lower(Transaction.merchant_key).op("~")(pattern))
        .distinct()
    ).all()
    # Prefer the name sharing the most words with the description.
    scored = sorted(rows, key=lambda r: -sum(1 for w in words if w in r[0].lower()))
    return [name for (name,) in scored][:limit]


def reconcile_month(session: Session, year: int, month: int) -> list[Match]:
    """Expected vs actual for every current fixed cost, in one month."""
    period = session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.year == year, BudgetPeriod.month == month
        )
    )

    costs = session.scalars(
        select(FixedCost)
        .where(FixedCost.effective_to.is_(None), FixedCost.parent_id.is_(None))
        .order_by(FixedCost.amount.desc())
    ).all()

    out: list[Match] = []
    for fc in costs:
        note: str | None = None
        merchant_name: str | None = None
        actual: Decimal | None = None
        charges = 0
        suggestions: list[str] = []

        if fc.merchant_key is not None:
            merchant_name = fc.merchant_key
        else:
            key = normalize_merchant(fc.description)
            if key:
                merchant_name = _key_for_description(session, key)

        if period is None:
            note = f"no data for {year}-{month:02d}"
        elif merchant_name is None:
            # No category fallback. Comparing rent against every rent-category
            # transaction produces a number that looks like an answer and is
            # not one; an honest blank invites the link that fixes it forever.
            note = "not linked to a merchant yet"
            suggestions = suggest_merchants(session, fc.description)
        else:
            total, count = session.execute(
                select(
                    func.coalesce(func.sum(Transaction.amount), 0),
                    func.count(Transaction.id),
                ).where(
                    Transaction.period_id == period.id,
                    Transaction.merchant_key == merchant_name,
                )
            ).one()
            actual, charges = Decimal(total), count
            if charges == 0:
                note = "expected but not charged this month"

        out.append(
            Match(
                fixed_cost_id=fc.id,
                description=fc.description,
                category=fc.category.name,
                expected=fc.amount,
                actual=actual.quantize(Decimal("0.01")) if actual is not None else None,
                merchant=merchant_name,
                charges=charges,
                note=note,
                suggestions=suggestions,
            )
        )
    return out


def recurring_candidates(session: Session) -> list[tuple[int, str, str]]:
    """Transactions that a fixed cost says are recurring but aren't marked.

    `is_recurring` is under-recorded in the imported history — rent is flagged
    on 3 of 19 rows — which makes the committed figure a floor rather than a
    split. Anything charged by a merchant that a standing commitment names is a
    safe candidate.

    Returns (transaction id, description, which fixed cost matched).
    """
    # An explicit link wins over the description, exactly as reconcile_month
    # treats it. Reading only the description missed every commitment whose
    # bill is named differently from its charge — rent, phone, the paper — and
    # those are the standing commitments, so the backfill skipped precisely the
    # rows it exists to find.
    by_merchant: dict[str, str] = {}
    unlinked: dict[str, str] = {}
    for fc in session.scalars(
        # Top-level only. A component is part of its parent's bill, and its
        # description is a line on an invoice ('Internet', 'Valet Trash'), not
        # the name of anyone who charges you.
        select(FixedCost).where(
            FixedCost.effective_to.is_(None), FixedCost.parent_id.is_(None)
        )
    ).all():
        if fc.merchant_key is not None:
            by_merchant.setdefault(fc.merchant_key, fc.description)
            continue
        key = normalize_merchant(fc.description)
        if key:
            unlinked.setdefault(key, fc.description)

    if unlinked:
        # Match the commitment's normalized description against the merchant
        # names actually in use, so 'Netflix' finds the rows filed under
        # 'Netflix' without a pattern table in between.
        for (name,) in session.execute(
            select(Transaction.merchant_key)
            .where(Transaction.merchant_key.is_not(None))
            .where(func.lower(Transaction.merchant_key).in_(unlinked))
            .distinct()
        ).all():
            by_merchant.setdefault(name, unlinked[name.lower()])

    if not by_merchant:
        return []

    rows = session.execute(
        select(Transaction.id, Transaction.raw_description, Transaction.merchant_key)
        .where(
            Transaction.merchant_key.in_(by_merchant),
            ~Transaction.is_recurring,
        )
        .order_by(Transaction.id)
    ).all()
    return [
        (txn_id, description, by_merchant[merchant_key])
        for txn_id, description, merchant_key in rows
    ]
