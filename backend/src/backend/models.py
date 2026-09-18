"""SQLAlchemy models.

Every table lives here so alembic/env.py picks the whole schema up with a
single import. A model that never reaches this module is invisible to
autogenerate.

Rewritten 2026-09-18. The previous schema had nineteen tables and grew out of
an Excel workbook: budget periods because the workbook had one sheet per month,
effective-dated fixed costs to model a commitment whose amount changes, parent
and child rows to express that rent includes utilities. Measured against three
years of real data, none of it earned its place — no fixed cost was ever
superseded, and budgets stopped being maintained a year in.

What is left stores only facts, and only facts an institution reported. Every
rollup — spending by category, net worth over time, budget versus actual — is
derived on read.
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Money is NUMERIC(12,2) everywhere, mapped to Decimal. Never float: the source
# workbook this app replaced contained visible binary-float error
# (1943.9000000000003), and it compounds through every sum.
Money = Numeric(12, 2)

# Units held, not dollars. Brokerages report fractional shares to six decimals,
# and a quantity rounded to cents is a position worth thousands less than it is.
Quantity = Numeric(20, 6)

# A share price is not money in the NUMERIC(12,2) sense — quotes carry four
# decimals, and rounding before multiplying by 163 shares moves real dollars.
Price = Numeric(14, 4)


class CategoryKind(str, enum.Enum):
    """Whether money in this category actually left.

    This exists because of a bug that took three commits to stamp out. A credit
    card payment is not spending — it is last month's spending, moving between
    two accounts you own — and counting it as spending made August's biggest
    purchase a $207.45 transfer that described nothing. Every total that claims
    to say "you spent this" filters on SPENDING and nothing else.

    What deliberately does *not* live here is committed-versus-flexible. That is
    a property of the individual charge, not of its category: a gym membership
    and a one-off massage are both Self Care.
    """

    SPENDING = "SPENDING"
    INCOME = "INCOME"
    # Between two accounts you own. Card payments, moving cash to savings.
    TRANSFER = "TRANSFER"
    # Also a transfer, but one worth seeing separately: money you still have.
    SAVINGS = "SAVINGS"


class SecurityKind(str, enum.Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    MUTUAL_FUND = "MUTUAL_FUND"
    FIXED_INCOME = "FIXED_INCOME"
    CASH = "CASH"
    OTHER = "OTHER"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    kind: Mapped[CategoryKind] = mapped_column(Enum(CategoryKind, name="category_kind"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<Category {self.name}>"


class Account(Base):
    """One account at one institution.

    Plaid's account and budgeter's account were two tables before, so that
    unlinking an institution could leave the account and its history behind.
    That mattered when most accounts were hand-made and only some were linked.
    Now every account arrives from Plaid, and the indirection only bought a
    join. `plaid_account_id` stays nullable so an account that no aggregator
    reaches can still be entered by hand.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Plaid's id for this account. Stable across refreshes; the natural key.
    plaid_account_id: Mapped[str | None] = mapped_column(
        String(120), unique=True, nullable=True
    )
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("plaid_items.id"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(80))
    institution: Mapped[str] = mapped_column(String(60))

    # The last four digits — for telling two cards at one bank apart, and for
    # recognising a payment to yourself, which quotes them.
    mask: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Plaid's own type/subtype, kept verbatim. `account_type` decides the one
    # thing that must never be guessed: whether a balance is owed or owned.
    account_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subtype: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Stored rather than derived from account_type, because it is the single
    # fact net worth turns on and a subtype nobody anticipated must not
    # silently flip a card balance into an asset.
    is_liability: Mapped[bool] = mapped_column(Boolean, default=False)

    # Retirement money is yours but not reachable. Net worth counts it; "what
    # could I actually spend" does not.
    is_retirement: Mapped[bool] = mapped_column(Boolean, default=False)

    # Set when an account is closed. Its balance history stays — a paid-off
    # loan is part of how net worth got here — but it stops being reported as
    # though its last reading were current.
    closed_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    item: Mapped["PlaidItem | None"] = relationship(back_populates="accounts")
    balances: Mapped[list["Balance"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Account {self.institution} {self.name}>"


class Balance(Base):
    """What one account was worth on one day.

    One row per account per day, which makes net worth over time a single
    `sum(amount) group by as_of` instead of the thing the workbook did (a new
    *column* per snapshot, which is why it had eight readings in two years).

    Liabilities are stored **negative**, so net worth is a plain sum and no
    caller has to remember the sign rule. That decision belongs here, once, at
    the point of writing — not in every query that reads it.
    """

    __tablename__ = "balances"
    __table_args__ = (UniqueConstraint("account_id", "as_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    as_of: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)

    account: Mapped[Account] = relationship(back_populates="balances")


class Transaction(Base):
    """One charge, as the bank reports it.

    **Sign convention is Plaid's, unchanged: positive means money left the
    account.** A refund and a paycheck are negative. This is copied rather than
    normalised on the way in because a conversion is a place to be wrong, and
    being wrong here is silent — it does not raise, it just reports a number.
    Every display that wants "you spent $X" flips the sign at the edge.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_occurred_on", "occurred_on"),
        Index("ix_transactions_category", "category_id", "occurred_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Plaid's transaction id, and the reason this is not a content hash: a
    # pending coffee posts two days later with a different date, often a
    # different amount, and usually a rewritten descriptor. A hash sees a brand
    # new transaction where there is one charge.
    plaid_transaction_id: Mapped[str | None] = mapped_column(
        String(120), unique=True, nullable=True
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))

    occurred_on: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)

    # What the bank sent, kept verbatim, and what it resolved to. Both, because
    # the descriptor is the evidence and the merchant is the answer — and the
    # answer is editable.
    description: Mapped[str] = mapped_column(String(200))
    merchant: Mapped[str | None] = mapped_column(String(120), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship()
    category: Mapped[Category] = relationship()


class Budget(Base):
    """What one category was meant to cost in one month.

    `month` is a date pinned to the first of the month, not a foreign key to a
    periods table. The old schema had one, inherited from a workbook with one
    sheet per month; it meant a budget could not be written until its month had
    been created, which is a rule with no reason behind it.
    """

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("category_id", "month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    month: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)

    category: Mapped[Category] = relationship()


class Security(Base):
    """Something that can be held.

    Keyed by Plaid's security id. The previous version of this table keyed on
    the ticker and carried a `quote_symbol` column, because the largest holding
    in the portfolio — a collective investment trust sold only inside employer
    plans — has no public price and had to borrow another fund's movement. The
    institution reports its own price now, so nothing is inferred and that
    column is gone.
    """

    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(primary_key=True)
    plaid_security_id: Mapped[str] = mapped_column(String(120), unique=True)

    name: Mapped[str] = mapped_column(String(120))
    ticker: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cusip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kind: Mapped[SecurityKind] = mapped_column(
        Enum(SecurityKind, name="security_kind"), default=SecurityKind.OTHER
    )

    # Money market funds and sweep balances. Worth knowing because they are
    # cash wearing a ticker, and showing them as investments overstates how
    # much of a portfolio is actually at risk.
    is_cash_equivalent: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<Security {self.ticker or self.name}>"


class Holding(Base):
    """What one account held of one security on one day.

    Dated, like balances, so a position is a series rather than a current
    value that overwrites its own history.

    `price` and `value` are the institution's own figures, stored as reported.
    The rule the old design rested on — never write an estimate — survives
    intact and is now nearly free: there is no estimate left to write.
    """

    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("account_id", "security_id", "as_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"))
    as_of: Mapped[date] = mapped_column(Date)

    quantity: Mapped[Decimal] = mapped_column(Quantity)
    price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    value: Mapped[Decimal] = mapped_column(Money)

    # What was paid in. Nullable because not every institution reports it, and
    # a missing cost basis has to read as "not reported" rather than as zero —
    # zero would report the entire position as gain.
    cost_basis: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    account: Mapped[Account] = relationship()
    security: Mapped[Security] = relationship()


class PlaidItem(Base):
    """One linked institution. Plaid calls it an Item.

    Chase is a single Item however many cards and checking accounts sit behind
    it, so this — not the account — is what gets re-authenticated, re-synced
    and unlinked.
    """

    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(String(120), unique=True)
    institution_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    institution_name: Mapped[str] = mapped_column(String(60))

    # Fernet-encrypted, never the raw token. A Plaid access token is a
    # long-lived read key to a real bank account, and this database gets dumped
    # and copied around like ordinary data.
    access_token: Mapped[str] = mapped_column(Text)

    # Which products this item was created with. Recorded because it cannot be
    # changed afterwards: adding `investments` to a transactions-only item
    # requires running Link again in update mode with fresh consent, so
    # knowing what an item can answer is not something to find out by asking.
    products: Mapped[str] = mapped_column(String(120), default="transactions")

    # How far /transactions/sync has been consumed. Advanced only when a sync
    # is *committed*, never when one is previewed — abandoning a preview has to
    # re-offer the same rows next time rather than step past them for good.
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Where the last preview got to, held here rather than in the response so a
    # commit advances to exactly where the reviewed batch ended even if the
    # browser was reloaded in between.
    pending_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Nothing dated before this is offered.
    sync_start_on: Mapped[date] = mapped_column(Date)

    # Set when Plaid reports the login has gone stale. The item stays — its
    # accounts and their history are still real — it just cannot sync until
    # Link is run again in update mode.
    needs_reauth: Mapped[bool] = mapped_column(Boolean, default=False)

    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    accounts: Mapped[list[Account]] = relationship(back_populates="item")

    @property
    def product_list(self) -> list[str]:
        return [p for p in self.products.split(",") if p]

    def __repr__(self) -> str:
        return f"<PlaidItem {self.institution_name}>"


class AppUser(Base):
    """The one person who uses this app.

    A table rather than a pair of environment variables, because the failed
    login count and the lockout have to survive a restart — an attacker who can
    restart the process could otherwise reset the counter by doing so. There is
    deliberately no `role` column and no second row.
    """

    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)

    # Argon2id. Never reversible, never logged.
    password_hash: Mapped[str] = mapped_column(Text)

    # Fernet-encrypted with the same key as the Plaid tokens. A TOTP secret is
    # a bearer credential: anyone holding it can mint valid codes forever, so
    # storing it in the clear would make the second factor decorative against
    # exactly the attacker it exists to stop — one who has read the database.
    totp_secret: Mapped[str] = mapped_column(Text)

    # Set once the first valid code is entered, so a half-finished enrolment
    # cannot lock the account behind an authenticator that was never scanned.
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    recovery_codes: Mapped[list["RecoveryCode"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    passkeys: Mapped[list["Passkey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RecoveryCode(Base):
    """One single-use way back in when the authenticator is gone.

    Hashed like a password, because a recovery code *is* a password — it alone
    completes a login. Shown exactly once, at enrolment.
    """

    __tablename__ = "recovery_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))
    code_hash: Mapped[str] = mapped_column(Text)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[AppUser] = relationship(back_populates="recovery_codes")


class Passkey(Base):
    """A credential the device holds, unlocked by Touch ID.

    Multi-factor on its own: the private key never leaves the machine (what you
    have) and the authenticator will not use it without a fingerprint or the
    device password (what you are, or know). That is why signing in with one
    asks for nothing else — a password prompt in front of it would be theatre.

    Nothing secret is stored here. The public key verifies signatures and
    cannot produce them.
    """

    __tablename__ = "passkeys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))

    # Base64url, as the browser reports it.
    credential_id: Mapped[str] = mapped_column(String(500), unique=True)
    public_key: Mapped[str] = mapped_column(Text)

    # The authenticator's own counter. It must never go backwards: a lower
    # count than last time means the credential has been cloned, which is the
    # one thing this protocol can detect and must refuse.
    sign_count: Mapped[int] = mapped_column(Integer, default=0)

    label: Mapped[str] = mapped_column(String(60))
    transports: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[AppUser] = relationship(back_populates="passkeys")
