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


def _trimmed(value: Decimal) -> str:
    """A share count without the trailing zeros a NUMERIC(20,6) column adds.

    5460.762000 reads as false precision. Done by hand rather than with
    Decimal.normalize(), which turns a round 100 into 1E+2.
    """
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# Shares, not money. Fractional to six places, because a position rounded to
# cents is thousands of dollars adrift by the time it is multiplied by a price.
Quantity = Annotated[
    Decimal, PlainSerializer(_trimmed, return_type=str, when_used="json")
]

# A share price, which carries four decimals where money carries two. Rounding
# 35.5975 to 35.60 before multiplying by 163 shares moves the answer by a
# dollar, and a portfolio total that disagrees with the brokerage by a dollar
# is a portfolio total nobody trusts.
Price = Annotated[
    Decimal,
    PlainSerializer(lambda v: f"{v:.4f}", return_type=str, when_used="json"),
]


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: CategoryKind
    sort_order: int


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


class LiveNetWorthOut(BaseModel):
    """Net worth twice: as measured, and with securities repriced.

    Never one without the other. The measured figure is the one that is true
    and slightly old; the estimate is the one that is current and slightly
    uncertain, and a screen that shows only the second has no way to answer
    "since when?".
    """

    # When everything was last actually read. Null before any snapshot exists.
    measured_on: date | None
    measured: Money
    measured_retirement: Money
    measured_liquid: Money

    estimated: Money
    estimated_retirement: Money
    estimated_liquid: Money
    # The market's contribution since that date — the whole difference between
    # the two figures above.
    change: Money
    is_estimated: bool

    # When the prices behind the estimate were fetched, ISO-8601.
    priced_at: str | None
    # How many accounts were repriced, and how many are simply their last
    # reading. A checking balance cannot be marked to market, so an estimate
    # covering nine accounts of which two moved should say exactly that.
    marked_accounts: int
    carried_accounts: int
    warnings: list[str]


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
    merchant_key: str | None
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
    # Who was paid. Omit it and one is guessed from the description; pass an
    # empty string to say this row has no merchant and mean it.
    merchant_key: str | None = Field(default=None, max_length=120)
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
    # Blank or null clears the merchant; any other value snaps to the spelling
    # already in use so editing cannot create a near-duplicate name.
    merchant_key: str | None = Field(default=None, max_length=120)
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
