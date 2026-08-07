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
    transaction_count: int = 0


class MerchantMergeIn(BaseModel):
    into_id: int = Field(description="The merchant that survives the merge")


class MerchantRenameIn(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=120)


class CategoryMix(BaseModel):
    name: str
    count: int


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    institution: str
    name: str
    is_retirement: bool
    closed_on: date | None
    latest_balance: Money | None
    latest_as_of: date | None
    # True when this account did not report on the most recent snapshot date,
    # so its balance is history rather than a current position. A two-year-old
    # loan balance presented as "latest" reads as money still owed.
    is_stale: bool
    days_behind: int | None
    # Movement since the previous snapshot — the only number that says whether
    # the account is going the right way.
    change: Money | None
    snapshot_count: int


class AccountIn(BaseModel):
    institution: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=60)
    is_retirement: bool = False


class AccountPatch(BaseModel):
    institution: str | None = Field(default=None, min_length=1, max_length=60)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    is_retirement: bool | None = None
    # Pass a date to settle the account, or null to reopen it.
    closed_on: date | None = None


class BalanceIn(BaseModel):
    as_of: date
    balance: Decimal


class BalanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    as_of: date
    balance: Money


class NetWorthPoint(BaseModel):
    as_of: date
    net_worth: Money
    retirement: Money
    liquid: Money
    # How many accounts reported on this date. A total built from five accounts
    # is not comparable to one built from eight, and the chart should say so
    # rather than drawing a cliff.
    accounts_reported: int


class NetWorthOut(BaseModel):
    points: list[NetWorthPoint]
    accounts_tracked: int


class MerchantRow(BaseModel):
    """A merchant as it appears on the workbench: what it cost and when."""

    id: int
    canonical_name: str
    transaction_count: int
    total_spent: Money
    last_seen: date | None
    categories: list[CategoryMix]


class MergeManyIn(BaseModel):
    """Fold several merchants into one, optionally renaming the survivor.

    Needed because the suggestion rule keys on the first word, so it can never
    propose 'Airbnb', 'Future Rent Airbnb' and 'Revolution Park Air Bnb' as one
    place. Those have to be picked by hand.
    """

    source_ids: list[int] = Field(min_length=1)
    into_id: int
    canonical_name: str | None = Field(default=None, max_length=120)


class SuggestionMember(BaseModel):
    """One merchant inside a proposal, with the descriptors behind it.

    The raw descriptors are the point: 'Rhino Mart' and 'Rhino Market Deli'
    are indistinguishable as names, but seeing what the bank actually wrote
    is what makes the call obvious.
    """

    id: int
    canonical_name: str
    transaction_count: int
    examples: list[str]


class Suggestion(BaseModel):
    key: str
    members: list[SuggestionMember]
    total_transactions: int


class RejectIn(BaseModel):
    """Record that names are different places.

    With `anchor`, only anchor-to-each pairs are recorded. That is the partial
    case: after merging 'Uber Eats' and 'Uber Eat', the leftovers are known to
    differ *from Uber Eats*, but nothing has been decided about whether
    'Uber Trip' and 'Uber To Airport' are each other.
    """

    names: list[str] = Field(min_length=1)
    anchor: str | None = None


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
    # Which account the money moved through. Null on the imported workbook
    # history, which never recorded one.
    account_id: int | None
    account_name: str | None
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
    account_id: int | None = None

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
    # Pass an id to attribute the row to an account, or null to clear it.
    account_id: int | None = None


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
