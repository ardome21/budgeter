"""Pydantic models for the API boundary.

Money crosses the wire as a **string**, never a number. JavaScript has no
decimal type, so a JSON number becomes a float the moment it is parsed, and
arithmetic on floats is how a budget ends up three cents off with no
explanation. Every sum happens in Postgres or in Python with Decimal; the
browser formats what it is given and never adds anything up.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from .models import CategoryKind, SecurityKind

Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json"),
]


def _trimmed(value: Decimal) -> str:
    """A share count without the trailing zeros a NUMERIC(20,6) column adds.

    5460.762000 reads as false precision. Done by hand rather than with
    Decimal.normalize(), which turns a round 100 into 1E+2.
    """
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


Quantity = Annotated[
    Decimal, PlainSerializer(_trimmed, return_type=str, when_used="json")
]

Price = Annotated[
    Decimal,
    PlainSerializer(lambda v: f"{v:.4f}", return_type=str, when_used="json"),
]


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- categories -------------------------------------------------------------


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    kind: CategoryKind = CategoryKind.SPENDING
    sort_order: int = 0


class CategoryOut(Base):
    id: int
    name: str
    kind: CategoryKind
    sort_order: int
    transaction_count: int = 0


class RegularCharge(BaseModel):
    merchant: str
    # As it last came, and on which day of the month.
    amount: Money
    day: int
    category_id: int
    category_name: str
    # Filed by your merchant rule rather than by the bank's label.
    ruled: bool


class MerchantRuleIn(BaseModel):
    merchant: str = Field(min_length=1, max_length=120)
    # None clears the rule, and the rows it moved go back to the bank's label.
    category_id: int | None


class MerchantRuleOut(BaseModel):
    merchant: str
    category_id: int | None
    # How many rows were re-filed to match.
    moved: int


# --- accounts ---------------------------------------------------------------


class AccountOut(Base):
    id: int
    name: str
    institution: str
    mask: str | None
    account_type: str | None
    subtype: str | None
    is_liability: bool
    is_retirement: bool
    closed_on: date | None
    # checking, savings, credit, loan, retirement, investment or other. See
    # `Account.group`.
    group: str
    balance: Money | None = None
    balance_as_of: date | None = None


class AccountPatch(BaseModel):
    is_retirement: bool | None = None
    closed_on: date | None = None


# --- transactions -----------------------------------------------------------


class TransactionOut(Base):
    id: int
    occurred_on: date
    description: str
    merchant: str | None
    # Plaid's sign, unchanged: positive means money left the account.
    amount: Money
    category_id: int
    category_name: str
    category_kind: CategoryKind
    account_id: int
    account_name: str
    notes: str | None
    # Authorised, not posted: counted in no total until it is.
    pending: bool = False
    # How many *other* rows moved with this one, when the edit was applied to
    # the whole merchant. Zero on an ordinary edit.
    also_filed: int = 0


class TransactionPage(BaseModel):
    rows: list[TransactionOut]
    total: int


class TransactionPatch(BaseModel):
    category_id: int | None = None
    merchant: str | None = None
    notes: str | None = None
    # Apply this category to every other row from the same merchant that is
    # still unfiled. Filing Uber Eats once should not mean filing it ten times.
    # Never touches a row already filed by hand — see `_file_merchant`.
    apply_to_merchant: bool = False


class RefileOut(BaseModel):
    moved: int
    # Rows nothing could place. Reported rather than hidden: it is the number
    # that says how much is left to do by hand.
    left: int


# --- budgets ----------------------------------------------------------------


class BudgetIn(BaseModel):
    category_id: int
    amount: Money


class BudgetOut(Base):
    category_id: int
    amount: Money


class BudgetPlanRow(BaseModel):
    category_id: int
    category_name: str
    # In force now: this month's own, or carried forward. None is no target.
    budget: Money | None
    last_month: Money
    # The median of the last three complete months, not the mean — one flight
    # should not set the Travel budget for the year.
    typical: Money
    # How many of those three months it appeared in. Seen once in three is a
    # different proposal from seen every month.
    months_seen: int
    # Each figure rounded up to the next $10; None where nothing was spent.
    from_last_month: Money | None
    from_typical: Money | None


class BudgetPlan(BaseModel):
    month: date
    # Which month set the budget in force; None when none has ever been set.
    budget_from: date | None
    last_month: date
    last_month_spent: Money
    last_month_income: Money
    typical_spent: Money
    rows: list[BudgetPlanRow]
    # Income categories, positive, proposals rounded down rather than up.
    income: list[BudgetPlanRow]


# --- the month --------------------------------------------------------------


class CategorySpend(BaseModel):
    category_id: int
    category_name: str
    kind: CategoryKind
    spent: Money
    budget: Money | None
    # Present only when there is a budget. Null means "no target", which is a
    # different thing from "nothing left" and must not render as an empty bar.
    remaining: Money | None
    # What an evenly-spread budget allows by today. Null with no budget.
    expected_to_date: Money | None = None
    # Decided server-side, in Decimal. The browser compares booleans rather
    # than two money strings — it has no decimal type and nothing here adds up.
    over_budget: bool = False
    over_pace: bool = False
    # Spending only. Last month to the same day while this one is running, or
    # the whole of it once this one is over; and this month less that.
    previous: Money | None = None
    change: Money | None = None
    merchants: list["MerchantSpend"] = []


class MerchantSpend(BaseModel):
    name: str
    spent: Money
    count: int


class BigPurchase(BaseModel):
    id: int
    occurred_on: date
    merchant: str
    category_name: str
    amount: Money


class UpcomingCharge(BaseModel):
    merchant: str
    category_name: str
    amount: Money
    expected_on: date
    # Its day has passed and nothing like it has posted.
    late: bool


class MonthOut(BaseModel):
    month: date
    # The denominator of every pace figure. A past month is complete, a future
    # one has not started, the current one is elapsed through today inclusive.
    days_in_month: int
    days_elapsed: int
    # What the spending budgets allow by today. Null when nothing is budgeted:
    # "on track" against no target is not a claim worth making.
    expected_to_date: Money | None = None
    over_budget: bool = False
    over_pace: bool = False
    spent: Money
    income: Money
    saved: Money
    # Transfers are reported separately and counted in nothing else. A card
    # payment is last month's spending moving between accounts you own, and
    # adding it to this month's total is a bug that took three commits to kill.
    transferred: Money
    budgeted: Money
    # The income budget in force, positive. None when none is set.
    income_expected: Money | None = None
    # Money in less spending, as it stands.
    left: Money
    # Once the expected pay lands: max(received, expected) less spending.
    left_expected: Money | None = None
    # Expected pay less the spending budget: what the plan leaves.
    left_planned: Money | None = None
    # The month whose budget this is: this one, or the last one set before it.
    budget_from: date | None = None
    categories: list[CategorySpend]
    transaction_count: int
    previous_month: date
    # The day of last month the comparison runs to; None for the whole month.
    compared_through: int | None = None
    previous_spent: Money
    # Spending so far at the end of each day: this month through today, and
    # the whole of last month.
    daily: list[Money] = []
    previous_daily: list[Money] = []
    biggest: list[BigPurchase] = []
    # Regular charges that have come every month and not yet this one.
    upcoming: list[UpcomingCharge] = []
    upcoming_total: Money
    # Spending authorised but not posted, in no other figure.
    pending_total: Money = Decimal("0.00")
    pending_count: int = 0
    # Left, less what is pending and what is still to come.
    left_after_upcoming: Money


# --- net worth --------------------------------------------------------------


class DayAccount(BaseModel):
    account_id: int
    name: str
    amount: Money
    # bank, archive or rebuilt — see `Balance.source`.
    source: str


class DayGroup(BaseModel):
    # See `Account.group`.
    group: str
    total: Money
    accounts: list[DayAccount]


class NetWorthPoint(BaseModel):
    as_of: date
    assets: Money
    liabilities: Money
    net: Money
    # How many accounts reported on this date. A day when only two of eleven
    # accounts were read is not a day net worth halved, and the series has to
    # be able to say so.
    accounts_reporting: int
    # Whether any figure on this day was rebuilt rather than read. See
    # `backend.rebuild`.
    estimated: bool = False
    # What the day is made of, for the chart's hover.
    groups: list[DayGroup] = []


class NetWorthOut(BaseModel):
    points: list[NetWorthPoint]
    latest: NetWorthPoint | None
    account_count: int


# --- holdings ---------------------------------------------------------------


class HoldingOut(BaseModel):
    security_id: int
    name: str
    ticker: str | None
    kind: SecurityKind
    is_cash_equivalent: bool
    account_id: int
    account_name: str
    quantity: Quantity
    price: Price | None
    value: Money
    cost_basis: Money | None
    # value - cost_basis, or null when the institution did not report a basis.
    # Null is not zero: zero would claim the whole position is gain.
    gain: Money | None


class PortfolioOut(BaseModel):
    as_of: date | None
    total_value: Money
    total_cost_basis: Money | None
    # How much of `total_value` the basis figure actually accounts for. Without
    # it "paid in" reads as covering the whole portfolio, and here it covers
    # under a quarter of it.
    cost_basis_covers: Money = Decimal("0.00")
    holdings: list[HoldingOut]


# --- linked banks -----------------------------------------------------------


class LinkedAccountOut(BaseModel):
    id: int
    name: str
    mask: str | None
    # See `Account.group`.
    group: str
    # The bank's own latest figure, never a rebuilt one.
    balance: Money | None = None
    # Set when hidden. Its history stays; it just stops being reported.
    closed_on: date | None = None
    newest_transaction: date | None = None
    oldest_transaction: date | None = None
    transaction_count: int = 0
    # Written in the last seven days, by any refresh.
    new_this_week: int = 0


class LinkedItemOut(BaseModel):
    id: int
    institution_name: str
    products: str
    needs_reauth: bool
    sync_start_on: date
    last_synced_at: str | None = None
    account_count: int = 0
    accounts: list[LinkedAccountOut] = []


class RefreshOut(BaseModel):
    added: int
    modified: int
    removed: int
    accounts_updated: int
    holdings_updated: int
    # Banks that could not be reached, named. One 502 for the lot hides which
    # bank is the problem and makes a stale login look like a broken app.
    failures: list[str] = []
