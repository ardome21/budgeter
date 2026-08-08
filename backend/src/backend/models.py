"""SQLAlchemy models.

Every table lives here (or is imported here) so that alembic/env.py picks the
whole schema up with a single import. A model that never reaches this module
is invisible to autogenerate.

Only facts are stored. Everything the source spreadsheet computed — monthly
overview, category rollups, budget-vs-actual, committed-vs-flexible — is
derived on read, so it can never go stale the way the workbook's did.
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    text,
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

    # Money moving between accounts you already own, and money arriving. Both
    # exist only because linking a *checking* account brought them in: the
    # workbook only ever recorded spending, so nothing before this needed to
    # say "this is not an outflow".
    #
    # Paying a credit card off is the case that forced it. The payment leaves
    # checking and the card's own purchases arrive separately, so counting the
    # payment as spending counts the same money twice — $207.45 of one August,
    # 8% of the month, on the first day of real data.
    TRANSFER = "TRANSFER"
    INCOME = "INCOME"


class TransactionSource(str, enum.Enum):
    WORKBOOK = "WORKBOOK"  # one-shot import of the Excel history
    CSV = "CSV"  # bank export
    MANUAL = "MANUAL"  # typed in by hand
    LINKED = "LINKED"  # pulled from the bank over Plaid


class PaycheckLineKind(str, enum.Enum):
    INCOME = "INCOME"
    INSURANCE = "INSURANCE"
    SAVINGS = "SAVINGS"
    TAX = "TAX"


class SecurityKind(str, enum.Enum):
    """What a holding is, which is what decides how it can be priced.

    Not decoration. Each value is a different pricing rule, and getting the
    rule wrong is worse than having no price at all:

    - EQUITY, ETF, MUTUAL_FUND quote publicly. Value is quantity x price.
    - MONEY_MARKET is a dollar. It never moves, so it is never fetched — a
      quote provider that returns 0.9998 for SPAXX would otherwise wobble a
      cash balance that is, by construction, exactly its own number.
    - UNQUOTED has no public price and never will. The Moody's PPP holding is
      a collective investment trust sold only inside employer plans: no
      ticker, no provider, no exception. Its value comes from the statement,
      or from a proxy's *movement* — see Security.quote_symbol.
    """

    EQUITY = "EQUITY"
    ETF = "ETF"
    MUTUAL_FUND = "MUTUAL_FUND"
    MONEY_MARKET = "MONEY_MARKET"
    UNQUOTED = "UNQUOTED"


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


# Units held, not dollars. Fidelity reports fractional shares to three decimals
# on some lines (5460.762) and four on others (12.8131), and a quantity rounded
# to cents is a position worth thousands of dollars less than it is.
Quantity = Numeric(20, 6)

# A share price is not money in the NUMERIC(12,2) sense. Intraday quotes carry
# four decimals (FDEM at 35.5975), and rounding the price before multiplying by
# 163 shares moves the answer by real dollars.
Price = Numeric(14, 4)


class Security(Base):
    """Something that can be held, and how to find out what it is worth.

    Keyed by the symbol as the statement writes it, because that is the only
    identifier the export gives and a second table of surrogate ids would buy
    nothing. `quote_symbol` is the whole point of this table:

    - equal to `symbol` — it quotes publicly under its own name, the ordinary
      case for a stock, an ETF or a mutual fund.
    - different from `symbol` — a **proxy**. The security itself has no public
      quote, so a similar one stands in and only its *movement* is borrowed.
      Never its price: FID FRDM INX 2065 T trades at $22.70 a unit and its
      public twin FFIJX at $19.93, so multiplying units by the proxy's price
      would understate that account by 14% on the first day.
    - null — nothing prices this. The holding is carried at its statement
      value and labelled as such, which is honest and stays honest.
    """

    __tablename__ = "securities"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    description: Mapped[str] = mapped_column(String(120))
    kind: Mapped[SecurityKind] = mapped_column(Enum(SecurityKind, name="security_kind"))

    # Where a price actually comes from. See the class docstring — the three
    # states here are three different pricing rules, not a nullable convenience.
    quote_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)

    def __repr__(self) -> str:
        return f"<Security {self.symbol}>"


class Holding(Base):
    """One position in one account on one date, exactly as the statement said.

    A snapshot, deliberately, in the same shape as AccountBalance: dropping a
    newer export adds rows rather than overwriting, so what was held in August
    survives September's rebalance. That history is the only way to tell money
    you added from money the market made.

    Everything here is a *fact off the statement*. Nothing computed from a live
    quote is ever written to this table — the marked-to-market figure is
    derived on read, so a bad afternoon quote can never become permanent.
    """

    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("account_id", "as_of", "symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    as_of: Mapped[date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(ForeignKey("securities.symbol"))

    # Null for a money-market line: the export gives the HSA's $7,790.53 with
    # no unit count at all, and inventing quantity = value would be asserting a
    # $1.00 par the statement never printed.
    quantity: Mapped[Decimal | None] = mapped_column(Quantity, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)

    # The statement's own 'Current value'. Authoritative, and the fallback for
    # every holding that cannot be priced any other way.
    value: Mapped[Decimal] = mapped_column(Money)

    # What it cost. The difference between this and value is the part of the
    # balance that was never saved — 41% of the retirement account, which no
    # amount of budgeting produced.
    cost_basis: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    account: Mapped[Account] = relationship()
    security: Mapped[Security] = relationship()


class SecurityPrice(Base):
    """A closing price on a date, cached.

    Deliberately not foreign-keyed to `securities`: the rows for FFIJX exist
    only because it stands in for a trust nobody holds directly, and a
    constraint would force a fake holding into existence to keep them.

    Cached because the proxy rule needs a price on the *snapshot* date as well
    as today, and refetching history on every page load would be both slow and
    a good way to get rate-limited out of the one provider that works.
    """

    __tablename__ = "security_prices"
    __table_args__ = (UniqueConstraint("symbol", "as_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    close: Mapped[Decimal] = mapped_column(Price)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BrokerageAccount(Base):
    """Ties an account number on a statement to an account in budgeter.

    The same job PlaidAccount does for a linked bank, and kept separate from
    `accounts` for the same reason: the account number is the brokerage's
    business, not budgeter's. Mapped once, on the first import, so no later
    export ever has to be pointed at the right rows by hand.
    """

    __tablename__ = "brokerage_accounts"
    __table_args__ = (
        UniqueConstraint("institution", "external_number"),
        UniqueConstraint("account_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    institution: Mapped[str] = mapped_column(String(60))
    external_number: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))

    # What the statement calls it ("ROTH IRA"), kept for the mapping screen so
    # an unfamiliar account number has a name beside it.
    external_name: Mapped[str | None] = mapped_column(String(60), nullable=True)

    account: Mapped[Account] = relationship()


class AppUser(Base):
    """The one person who uses this app.

    A table rather than a pair of environment variables, because the failed
    login count and the lockout have to survive a restart — an attacker who can
    restart the process could otherwise reset the counter by doing so. There is
    deliberately no `role` column and no second row: this is a single-user app,
    and inventing a permission model for one user would be pretending to a
    structure that does not exist.
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

    Without these, losing a phone means losing access to three years of your
    own financial history, and the only remaining way in is the break-glass
    script. That is a real enough risk to design for rather than warn about.
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
    asks for nothing else — a password prompt in front of it would be theatre,
    not a third factor.

    Nothing secret is stored here. The public key verifies signatures and
    cannot produce them, so unlike the TOTP secret this table is not worth
    encrypting — an attacker who reads it learns nothing they can sign with.
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

    # What the user calls it, so revoking the right one is possible when there
    # are several. "MacBook Touch ID", "iPhone".
    label: Mapped[str] = mapped_column(String(60))
    transports: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[AppUser] = relationship(back_populates="passkeys")


class PlaidItem(Base):
    """One linked institution. Plaid calls it an Item.

    Chase is a single Item however many cards and checking accounts sit behind
    it, so this — not the account — is the thing that gets re-authenticated,
    re-synced and unlinked.
    """

    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(String(120), unique=True)
    institution_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    institution_name: Mapped[str] = mapped_column(String(60))

    # Fernet-encrypted, never the raw token. A Plaid access token is a
    # long-lived read key to a real bank account, and this database gets dumped
    # and copied around like ordinary data. See Settings.plaid_token_key.
    access_token: Mapped[str] = mapped_column(Text)

    # How far /transactions/sync has been consumed. Advanced only when a sync
    # is *committed*, never when one is previewed — abandoning a preview has to
    # re-offer the same rows next time rather than skip past them for good.
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Where the last *preview* got to. Held here rather than in the response so
    # that the commit advances to exactly the point the reviewed batch ended,
    # even if the browser was reloaded in between. Copied over `cursor` on
    # commit and cleared.
    pending_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Nothing dated before this is offered. Plaid returns up to 24 months on a
    # first sync and the workbook already holds every one of those months, so
    # without a floor the first refresh is a thousand rows of near-duplicates
    # against hand-typed history.
    sync_start_on: Mapped[date] = mapped_column(Date)

    # Set when Plaid reports the login has gone stale. The item stays — its
    # accounts and their history are still real — it just cannot sync until
    # Link is run again in update mode.
    needs_reauth: Mapped[bool] = mapped_column(Boolean, default=False)

    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    accounts: Mapped[list["PlaidAccount"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class PlaidAccount(Base):
    """Ties one account at the bank to one account in budgeter.

    Kept as its own table rather than a column on `accounts` because the
    mapping is the part that is Plaid's business. Unlinking an institution
    drops these rows and leaves the accounts, their balances and their
    transactions exactly where they were.
    """

    __tablename__ = "plaid_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid_items.id"))
    plaid_account_id: Mapped[str] = mapped_column(String(120), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))

    # The last four digits, for telling two cards at one bank apart on screen —
    # and for recognising a payment to yourself, which quotes them.
    mask: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Plaid's own type/subtype. `account_type` decides two things that must not
    # be guessed: whether a balance is a liability (stored negative), and
    # whether a negative amount is a deposit or a refund.
    account_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subtype: Mapped[str | None] = mapped_column(String(40), nullable=True)

    item: Mapped[PlaidItem] = relationship(back_populates="accounts")
    account: Mapped[Account] = relationship()


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
        # Only for linked rows, where source_ref is Plaid's transaction id and
        # therefore an identity. For workbook rows the same column is a cell
        # reference — provenance, not a key — so the constraint is deliberately
        # not imposed on them.
        Index(
            "uq_transactions_linked_source_ref",
            "source_ref",
            unique=True,
            postgresql_where=text("source = 'LINKED'"),
        ),
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

    # Who was paid, as a name rather than a foreign key. Two rows are the same
    # place when this string matches, so the entry form picks from what already
    # exists instead of letting a second spelling in — which is what the merge
    # queue used to clean up afterwards.
    merchant_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True
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

    # Two jobs, decided by `source`. For a workbook row it is provenance — the
    # cell the figure came from. For a LINKED row it is Plaid's transaction id,
    # which is identity: the bank rewrites a charge when it posts (the date
    # moves, the amount settles, the descriptor is replaced), so a content hash
    # sees a new row where this correctly sees the same one.
    source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)

    period: Mapped[BudgetPeriod] = relationship()
    category: Mapped[Category] = relationship()
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
    merchant_key: Mapped[str | None] = mapped_column(String(120), nullable=True)

    category: Mapped[Category] = relationship()
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
