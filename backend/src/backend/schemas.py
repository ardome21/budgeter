"""Pydantic models for the API boundary.

Money crosses the wire as a **string**, never a number. JavaScript has no
decimal type, so a JSON number becomes a float the moment it is parsed, and
arithmetic on floats is how a budget ends up 3 cents off with no explanation.
Every sum happens in Postgres or in Python with Decimal; the browser formats
what it is given and never adds anything up.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from .models import CategoryKind, TransactionSource

Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json"),
]
# Ratios stay numbers — they are for bar widths, not for money.
Ratio = Annotated[float, Field(ge=-1000, le=1000)]


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: CategoryKind
    sort_order: int


class MerchantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    canonical_name: str
    default_category_id: int | None = None
    transaction_count: int = 0


class MerchantMergeIn(BaseModel):
    into_id: int = Field(description="The merchant that survives the merge")


class PeriodOut(BaseModel):
    year: int
    month: int
    transaction_count: int
    total: Money


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_on: date | None
    year: int
    month: int
    raw_description: str
    merchant_id: int | None
    merchant_name: str | None
    category_id: int
    category_name: str
    amount: Money
    is_recurring: bool
    source: TransactionSource


class TransactionIn(BaseModel):
    """A hand-entered transaction.

    `occurred_on` is optional to match the imported history, but when it is
    absent the caller must say which month it belongs to — a transaction with
    neither a date nor a period cannot be rolled up, so it is rejected rather
    than filed somewhere arbitrary.
    """

    occurred_on: date | None = None
    year: int | None = Field(default=None, ge=1990, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    raw_description: str = Field(min_length=1, max_length=200)
    category_id: int
    amount: Decimal
    is_recurring: bool = False

    @field_validator("amount")
    @classmethod
    def not_zero(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError("amount must not be zero")
        return v.quantize(Decimal("0.01"))

    def resolved_period(self) -> tuple[int, int]:
        if self.occurred_on is not None:
            return self.occurred_on.year, self.occurred_on.month
        if self.year is not None and self.month is not None:
            return self.year, self.month
        raise ValueError("provide occurred_on, or both year and month")


class TransactionPatch(BaseModel):
    occurred_on: date | None = None
    raw_description: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: int | None = None
    amount: Decimal | None = None
    is_recurring: bool | None = None


class CategoryLine(BaseModel):
    """One row of the month view — the budget sheet's category block."""

    category_id: int
    category: str
    kind: CategoryKind
    allocated: Money
    spent: Money
    remaining: Money
    pct_used: Ratio | None
    share_of_spend: Ratio


class CommitmentSplit(BaseModel):
    committed: Money
    flexible: Money
    saved: Money


class BiggestPurchase(BaseModel):
    description: str
    amount: Money
    occurred_on: date | None


class MonthDays(BaseModel):
    in_month: int
    elapsed: int
    remaining: int


class MonthSummary(BaseModel):
    year: int
    month: int
    categories: list[CategoryLine]
    allocated_total: Money
    spent_total: Money
    remaining_total: Money
    commitment: CommitmentSplit
    biggest: list[BiggestPurchase]
    days: MonthDays
    spent_per_day: Money | None
    safe_per_day: Money | None
    transaction_count: int
    undated_count: int


class OverviewOut(BaseModel):
    """The Monthly Overview sheet, recomputed rather than stored."""

    gross_monthly: Money
    post_tax: Money
    take_home: Money
    fixed_costs: Money
    disposable: Money
    auto_saved: Money
    paychecks_per_month: int
    fixed_by_category: list["FixedCostGroup"]


class FixedCostGroup(BaseModel):
    category: str
    amount: Money
    lines: list[str]
