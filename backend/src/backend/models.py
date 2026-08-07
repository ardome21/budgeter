"""SQLAlchemy models.

Every table lives here (or is imported here) so that alembic/env.py picks the
whole schema up with a single import. A model that never reaches this module
is invisible to autogenerate.

Only facts are stored. Everything the source spreadsheet computed — monthly
overview, category rollups, budget-vs-actual, committed-vs-flexible — is
derived on read, so it can never go stale the way the workbook's did.
"""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Money is NUMERIC(12,2) everywhere, mapped to Decimal. Never float: the source
# workbook already contains visible binary-float error (1943.9000000000003),
# and it compounds through every sum.
Money = Numeric(12, 2)


class CategoryKind(str, enum.Enum):
    """What a category *is* — not whether spending in it is committed.

    Committed-vs-flexible deliberately does not live here. It is a property of
    the individual transaction (`is_recurring`) and of the fixed-cost list, not
    of the category: Self Care holds both an $89/month gym membership and
    one-off purchases, and every committed-looking category in the history has
    discretionary rows in it too.

    What this does encode is whether money in the category left for good.
    Savings transfers are not spending, and summing them into a spending total
    is how a budget ends up looking worse than it is.
    """

    SPENDING = "SPENDING"
    SAVINGS = "SAVINGS"  # moved, not spent
    OTHER = "OTHER"  # Deficit Reduction — an accounting line, not an outflow


class TransactionSource(str, enum.Enum):
    WORKBOOK = "WORKBOOK"  # one-shot import of the Excel history
    CSV = "CSV"  # bank export
    MANUAL = "MANUAL"  # typed in by hand


class PaycheckLineKind(str, enum.Enum):
    INCOME = "INCOME"
    INSURANCE = "INSURANCE"
    SAVINGS = "SAVINGS"
    TAX = "TAX"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    kind: Mapped[CategoryKind] = mapped_column(Enum(CategoryKind, name="category_kind"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<Category {self.name}>"


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("institution", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(60))
    is_retirement: Mapped[bool] = mapped_column(Boolean, default=False)

    # Set when the account is settled or shut. The balance history stays — a
    # paid-off loan is still part of how net worth got here — but the account
    # stops being presented as though its last reading were current, and stops
    # being asked for on the next snapshot.
    closed_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    balances: Mapped[list["AccountBalance"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class AccountBalance(Base):
    """One snapshot of one account.

    The workbook stored these wide — a new column per snapshot date, which is
    why there were only eight in two years. As rows, snapshots are unbounded
    and net-worth-over-time is a single query.
    """

    __tablename__ = "account_balances"
    __table_args__ = (UniqueConstraint("account_id", "as_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    as_of: Mapped[date] = mapped_column(Date)
    balance: Mapped[Decimal] = mapped_column(Money)

    account: Mapped[Account] = relationship(back_populates="balances")


class Merchant(Base):
    """A real-world place, collapsing the many strings a bank uses for it.

    'Harris Teeter' arrives as eleven distinct descriptors across the history;
    resolving them to one merchant is what lets a category be inferred rather
    than typed.
    """

    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(120), unique=True)
    default_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), nullable=True
    )

    default_category: Mapped[Category | None] = relationship()
    patterns: Mapped[list["MerchantPattern"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )


class MerchantPattern(Base):
    """A normalized descriptor fragment that resolves to a merchant."""

    __tablename__ = "merchant_patterns"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"))
    pattern: Mapped[str] = mapped_column(String(120), unique=True)

    merchant: Mapped[Merchant] = relationship(back_populates="patterns")


class MerchantSplit(Base):
    """A pair the user has said are NOT the same place.

    Without this, saying "no" to a suggestion accomplishes nothing — the
    similarity rules would propose the same pair on every visit, and a review
    queue you cannot empty is one nobody works through.

    Keyed by name rather than id: merging deletes the losing merchant, so an
    id-based record would dangle. Names are stored in sorted order, so the
    pair (a, b) and (b, a) are the same row.
    """

    __tablename__ = "merchant_splits"
    __table_args__ = (UniqueConstraint("left_name", "right_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    left_name: Mapped[str] = mapped_column(String(120))
    right_name: Mapped[str] = mapped_column(String(120))


class BudgetPeriod(Base):
    """A calendar month. The unit everything rolls up to."""

    __tablename__ = "budget_periods"
    __table_args__ = (
        UniqueConstraint("year", "month"),
        CheckConstraint("month between 1 and 12", name="month_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)

    def __repr__(self) -> str:
        return f"<BudgetPeriod {self.year}-{self.month:02d}>"


class BudgetAllocation(Base):
    """The 'Budget Allotted' column. Used vs remaining is derived, never stored."""

    __tablename__ = "budget_allocations"
    __table_args__ = (UniqueConstraint("period_id", "category_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("budget_periods.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    amount: Mapped[Decimal] = mapped_column(Money)

    period: Mapped[BudgetPeriod] = relationship()
    category: Mapped[Category] = relationship()


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_period_category", "period_id", "category_id"),
        Index("ix_transactions_occurred_on", "occurred_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Nullable on purpose. Hundreds of rows in the workbook have a description
    # and an amount but no date — the date column was abandoned mid-month.
    # They are still real spending, and inventing a date would be a lie.
    occurred_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Required. Even an undated row belongs to a known month, because the sheet
    # it lived on names one. Rollups key off this, so they always work.
    period_id: Mapped[int] = mapped_column(ForeignKey("budget_periods.id"))

    raw_description: Mapped[str] = mapped_column(String(200))
    merchant_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchants.id"), nullable=True
    )
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))

    # Negative means money coming back — a refund reduces the category it came
    # from instead of hiding in a 'Refund' bucket.
    amount: Mapped[Decimal] = mapped_column(Money)

    # The workbook's 'Automatic?' column: recurring/committed vs discretionary.
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)

    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )
    source: Mapped[TransactionSource] = mapped_column(
        Enum(TransactionSource, name="transaction_source")
    )

    # Set for CSV imports so re-dropping the same export is a no-op. Left null
    # for hand-entered rows, which must never be deduplicated automatically —
    # two identical bar charges on one night are usually two real rounds.
    import_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )

    # Provenance for the one-shot workbook import, so any imported row can be
    # traced back to the cell it came from.
    source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)

    period: Mapped[BudgetPeriod] = relationship()
    category: Mapped[Category] = relationship()
    merchant: Mapped[Merchant | None] = relationship()
    account: Mapped[Account | None] = relationship()


class FixedCost(Base):
    """The 'Monthly Fixed Costs' sheet.

    effective_from means changing the rent keeps what it used to be, instead of
    overwriting history the way a spreadsheet cell does.
    """

    __tablename__ = "fixed_costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(Money)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    is_exact: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # A bill can carry its own breakdown. Rent arrives as one charge but is
    # really eleven lines — rent, admin fees, valet trash, amenity fee — and
    # when the charge moves, the breakdown is what says which line moved.
    # Components are part of the parent's amount, never counted alongside it.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("fixed_costs.id"), nullable=True
    )

    # Which merchant actually charges for this. Guessing from the description
    # gets Netflix right and rent wrong — the bill is called "Rent" and the
    # charge says BILT CARD HOUSING. Set once, correct forever.
    merchant_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchants.id"), nullable=True
    )

    category: Mapped[Category] = relationship()
    merchant: Mapped["Merchant | None"] = relationship()
    components: Mapped[list["FixedCost"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped["FixedCost | None"] = relationship(
        back_populates="components", remote_side="FixedCost.id"
    )


class PaycheckLine(Base):
    """One line of the paycheck breakdown: gross, a deduction, or a tax."""

    __tablename__ = "paycheck_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[PaycheckLineKind] = mapped_column(
        Enum(PaycheckLineKind, name="paycheck_line_kind")
    )
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
