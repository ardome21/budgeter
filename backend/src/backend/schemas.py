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


class TransactionPage(BaseModel):
    rows: list[TransactionOut]
    total: int


class TransactionPatch(BaseModel):
    category_id: int | None = None
    merchant: str | None = None
    notes: str | None = None


# --- budgets ----------------------------------------------------------------


class BudgetIn(BaseModel):
    category_id: int
    amount: Money


class BudgetOut(Base):
    category_id: int
    amount: Money


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


class MonthOut(BaseModel):
    month: date
    spent: Money
    income: Money
    saved: Money
    # Transfers are reported separately and counted in nothing else. A card
    # payment is last month's spending moving between accounts you own, and
    # adding it to this month's total is a bug that took three commits to kill.
    transferred: Money
    budgeted: Money
    categories: list[CategorySpend]
    transaction_count: int


# --- net worth --------------------------------------------------------------


class NetWorthPoint(BaseModel):
    as_of: date
    assets: Money
    liabilities: Money
    net: Money
    # How many accounts reported on this date. A day when only two of eleven
    # accounts were read is not a day net worth halved, and the series has to
    # be able to say so.
    accounts_reporting: int


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


class LinkedItemOut(Base):
    id: int
    institution_name: str
    products: str
    needs_reauth: bool
    sync_start_on: date
    last_synced_at: str | None = None
    account_count: int = 0


class PreviewRow(BaseModel):
    transaction_id: str
    occurred_on: date
    description: str
    merchant: str | None
    amount: Money
    account_id: int
    account_name: str
    # The guess, and where it came from. A suggestion nobody can check is a
    # suggestion nobody should accept.
    category_id: int | None
    category_name: str | None
    plaid_category: str | None


class PreviewOut(BaseModel):
    rows: list[PreviewRow]
    modified: int
    removed: int
    accounts_updated: int
    holdings_updated: int
    cursor_advanced: bool
    message: str | None = None


class CommitIn(BaseModel):
    item_id: int
    accept: list[str]
