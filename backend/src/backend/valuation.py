"""What the portfolio is worth now, and how confidently.

Every figure this module produces is **derived on read and never stored**. That
is the whole design. `account_balances` remains a record of things that were
actually measured — a statement downloaded, a bank refreshed — so the net worth
history cannot be polluted by an afternoon of bad quotes, and a number on the
chart is always something that was true rather than something that was
computed. What the screens show instead is the measured figure and the
estimate side by side, with the date the measured one was taken.

Four ways a holding gets a value, and each one is reported, because "your
portfolio is worth X" means something different in each case:

- `LIVE`     quantity x a public quote for this exact security.
- `PAR`      a money market. A dollar is a dollar; the statement figure stands.
- `PROXY`    no public quote, so the statement value is moved by the percentage
             change in a stand-in. Borrowing the proxy's *movement* and never
             its price is what makes this defensible — see `_by_proxy`.
- `CARRIED`  nothing could price it. The statement's own figure, unchanged,
             labelled as old rather than dressed up as current.
"""

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import prices
from .models import (
    Account,
    AccountBalance,
    Holding,
    Security,
    SecurityKind,
    SecurityPrice,
)

CENTS = Decimal("0.01")


class PricingBasis(str, enum.Enum):
    LIVE = "LIVE"
    PAR = "PAR"
    PROXY = "PROXY"
    CARRIED = "CARRIED"


@dataclass
class ValuedHolding:
    symbol: str
    description: str
    kind: SecurityKind
    quantity: Decimal | None
    cost_basis: Decimal | None

    # What the statement said, on the day it said it.
    statement_value: Decimal
    statement_price: Decimal | None

    # What it is worth now, by whichever rule applied.
    value: Decimal
    price: Decimal | None
    basis: PricingBasis
    priced_on: date | None = None
    proxy_symbol: str | None = None
    note: str | None = None

    @property
    def gain(self) -> Decimal | None:
        """Growth against what was paid — the part that was never saved."""
        return None if self.cost_basis is None else self.value - self.cost_basis

    @property
    def change(self) -> Decimal:
        """Movement since the statement. Zero for anything carried."""
        return self.value - self.statement_value


@dataclass
class ValuedAccount:
    account_id: int
    institution: str
    name: str
    is_retirement: bool
    as_of: date
    holdings: list[ValuedHolding] = field(default_factory=list)

    @property
    def statement_value(self) -> Decimal:
        return sum((h.statement_value for h in self.holdings), Decimal(0))

    @property
    def value(self) -> Decimal:
        return sum((h.value for h in self.holdings), Decimal(0))

    @property
    def cost_basis(self) -> Decimal | None:
        """Null unless every holding reports one — a partial sum is a lie."""
        if not self.holdings or any(h.cost_basis is None for h in self.holdings):
            return None
        return sum((h.cost_basis for h in self.holdings), Decimal(0))

    @property
    def gain(self) -> Decimal | None:
        basis = self.cost_basis
        return None if basis is None else self.value - basis

    @property
    def is_estimated(self) -> bool:
        """True when any part of this value came from a quote rather than the
        statement. Par and carried holdings are the statement's own figures."""
        return any(
            h.basis in (PricingBasis.LIVE, PricingBasis.PROXY) for h in self.holdings
        )


@dataclass
class Valuation:
    accounts: list[ValuedAccount] = field(default_factory=list)
    # What could not be priced, in the user's words rather than the provider's.
    warnings: list[str] = field(default_factory=list)
    priced_at: datetime | None = None

    @property
    def value(self) -> Decimal:
        return sum((a.value for a in self.accounts), Decimal(0))

    @property
    def statement_value(self) -> Decimal:
        return sum((a.statement_value for a in self.accounts), Decimal(0))

    @property
    def cost_basis(self) -> Decimal | None:
        if not self.accounts or any(a.cost_basis is None for a in self.accounts):
            return None
        return sum((a.cost_basis for a in self.accounts), Decimal(0))


def latest_holding_dates(session: Session) -> dict[int, date]:
    """The newest positions date on file for each account."""
    rows = session.execute(
        select(Holding.account_id, func.max(Holding.as_of)).group_by(
            Holding.account_id
        )
    ).all()
    return {account_id: as_of for account_id, as_of in rows}


def _quantized(value: Decimal) -> Decimal:
    return value.quantize(CENTS)


def _by_proxy(
    session: Session,
    holding: Holding,
    proxy: str,
    as_of: date,
) -> tuple[Decimal, PricingBasis, date | None, str | None]:
    """Move the statement value by the proxy's percentage change since `as_of`.

    The ratio is the point. The Moody's plan trust is quoted by Fidelity at
    $22.70 a unit while its public twin FFIJX trades at $19.93 — different
    share classes of the same strategy — so `quantity x proxy price` would
    report that account 14% light on the day the statement was downloaded.
    Dividing the proxy's price now by its price then cancels the level out and
    keeps only the move, which is the part the two genuinely share.

    Needs a price on *both* dates. With only one of them the honest answer is
    the statement figure, carried.
    """
    then = prices.price_on(session, proxy, as_of)
    now = prices.latest_price(session, proxy)
    if then is None or now is None or then[0] <= 0:
        return (
            holding.value,
            PricingBasis.CARRIED,
            None,
            f"no price for the stand-in {proxy} — showing the statement figure",
        )
    ratio = now[0] / then[0]
    return _quantized(holding.value * ratio), PricingBasis.PROXY, now[1], None


def _value_one(
    session: Session, holding: Holding, security: Security, as_of: date
) -> ValuedHolding:
    valued = ValuedHolding(
        symbol=holding.symbol,
        description=security.description,
        kind=security.kind,
        quantity=holding.quantity,
        cost_basis=holding.cost_basis,
        statement_value=holding.value,
        statement_price=holding.price,
        value=holding.value,
        price=holding.price,
        basis=PricingBasis.CARRIED,
    )

    if security.kind is SecurityKind.MONEY_MARKET:
        # A dollar is a dollar. Never fetched, so never wobbles.
        valued.basis = PricingBasis.PAR
        valued.price = prices.PAR
        if valued.cost_basis is None:
            # Cash bought at par cost exactly what it is worth. The statement
            # omits the column because there is no gain to report, not because
            # the figure is unknown — and treating it as unknown was enough to
            # null the cost basis of every account holding any cash, and with
            # it the one number this screen exists to show. Recorded here
            # rather than on the row: `holdings` stays exactly what the
            # statement said.
            valued.cost_basis = holding.value
        return valued

    quote_symbol = security.quote_symbol
    if not quote_symbol:
        valued.note = "no public quote — showing the statement figure"
        return valued

    if quote_symbol != holding.symbol:
        value, basis, priced_on, note = _by_proxy(
            session, holding, quote_symbol, as_of
        )
        valued.value = value
        valued.basis = basis
        valued.priced_on = priced_on
        valued.note = note
        valued.proxy_symbol = quote_symbol
        if basis is PricingBasis.PROXY and holding.quantity:
            valued.price = _quantized(value / holding.quantity)
        return valued

    quote = prices.latest_price(session, quote_symbol)
    if quote is None:
        valued.note = f"no price on file for {quote_symbol}"
        return valued
    if holding.quantity is None:
        # A price with nothing to multiply it by. Happens when the statement
        # omits the unit count, and the statement's own value is the better
        # answer than anything derivable from a price alone.
        valued.note = "the statement gave no quantity, so its value stands"
        return valued

    valued.value = _quantized(holding.quantity * quote[0])
    valued.price = quote[0]
    valued.priced_on = quote[1]
    valued.basis = PricingBasis.LIVE
    return valued


def symbols_to_refresh(session: Session) -> dict[str, date | None]:
    """Which symbols need a price, and how far back each one must reach.

    A directly quoted security only needs today. A proxy also needs its own
    close on the snapshot date, which may be months back, so it asks for
    history — the difference between one small request and one large one.
    """
    dates = latest_holding_dates(session)
    if not dates:
        return {}

    rows = session.execute(
        select(
            Holding.account_id,
            Holding.as_of,
            Holding.symbol,
            Security.quote_symbol,
            Security.kind,
        )
        .join(Security, Security.symbol == Holding.symbol)
        .where(Holding.account_id.in_(dates.keys()))
    ).all()

    wanted: dict[str, date | None] = {}
    for account_id, as_of, symbol, quote_symbol, kind in rows:
        # Only the current snapshot. Filtering on the set of dates in SQL would
        # let one account's newest date pull in another account's older rows.
        if as_of != dates[account_id]:
            continue
        if kind is SecurityKind.MONEY_MARKET or not quote_symbol:
            continue
        since = dates[account_id] if quote_symbol != symbol else None
        if quote_symbol in wanted:
            existing = wanted[quote_symbol]
            if since is not None and (existing is None or since < existing):
                wanted[quote_symbol] = since
        else:
            wanted[quote_symbol] = since
    return wanted


def value_portfolio(session: Session, *, refresh: bool = True) -> Valuation:
    """Value every account that has positions on file.

    `refresh=False` values from the cache alone — what the tests use, and what
    a rapid page reload gets, since the cache has its own freshness window.
    """
    out = Valuation()
    if refresh:
        _, errors = prices.refresh(session, symbols_to_refresh(session))
        out.warnings.extend(errors)

    dates = latest_holding_dates(session)
    if not dates:
        return out

    rows = session.execute(
        select(Holding, Security, Account)
        .join(Security, Security.symbol == Holding.symbol)
        .join(Account, Account.id == Holding.account_id)
        .where(Holding.account_id.in_(dates.keys()))
        .order_by(Account.institution, Account.name, Holding.symbol)
    ).all()

    by_account: dict[int, ValuedAccount] = {}
    for holding, security, account in rows:
        if holding.as_of != dates[holding.account_id]:
            continue
        valued_account = by_account.get(account.id)
        if valued_account is None:
            valued_account = ValuedAccount(
                account_id=account.id,
                institution=account.institution,
                name=account.name,
                is_retirement=account.is_retirement,
                as_of=holding.as_of,
            )
            by_account[account.id] = valued_account
        valued_account.holdings.append(
            _value_one(session, holding, security, holding.as_of)
        )

    out.accounts = list(by_account.values())

    out.priced_at = session.scalar(select(func.max(SecurityPrice.fetched_at)))
    return out


@dataclass
class LiveNetWorth:
    """Net worth with investments marked to market, and its own caveats.

    Built on top of the measured figure rather than beside it: the same set of
    accounts the Accounts screen counts, with the ones holding securities
    revalued and everything else left at its last reading. That keeps the
    estimate and the chart telling the same story, and makes the gap between
    them exactly the market movement since the last statement.
    """

    # The date everything was last actually measured. The answer to "when was
    # this exact?", which is the only thing that makes an estimate usable.
    measured_on: date | None
    measured: Decimal
    measured_retirement: Decimal
    measured_liquid: Decimal

    estimated: Decimal
    estimated_retirement: Decimal
    estimated_liquid: Decimal

    priced_at: datetime | None = None
    # Accounts revalued from live prices, and accounts left at their last
    # reading because nothing can price a checking balance.
    marked_accounts: int = 0
    carried_accounts: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def change(self) -> Decimal:
        return self.estimated - self.measured

    @property
    def is_estimated(self) -> bool:
        return self.marked_accounts > 0 and self.estimated != self.measured


def live_net_worth(session: Session, *, refresh: bool = True) -> LiveNetWorth:
    """The measured net worth, with securities repriced.

    Deliberately restricted to accounts that reported on the most recent
    snapshot date — the same rule the Accounts screen uses to decide what is
    current. Without it a settled loan or a closed card from two years ago
    walks back into the total the moment an estimate is asked for.
    """
    newest = session.scalar(select(func.max(AccountBalance.as_of)))
    if newest is None:
        return LiveNetWorth(
            measured_on=None,
            measured=Decimal(0),
            measured_retirement=Decimal(0),
            measured_liquid=Decimal(0),
            estimated=Decimal(0),
            estimated_retirement=Decimal(0),
            estimated_liquid=Decimal(0),
        )

    reported = session.execute(
        select(AccountBalance.account_id, AccountBalance.balance, Account.is_retirement)
        .join(Account, Account.id == AccountBalance.account_id)
        .where(AccountBalance.as_of == newest)
    ).all()

    valuation = value_portfolio(session, refresh=refresh)
    marked = {a.account_id: a for a in valuation.accounts}

    measured = measured_retirement = Decimal(0)
    estimated = estimated_retirement = Decimal(0)
    marked_count = carried_count = 0

    for account_id, balance, is_retirement in reported:
        valued = marked.get(account_id)
        current = valued.value if valued is not None else balance
        if valued is not None:
            marked_count += 1
        else:
            carried_count += 1

        measured += balance
        estimated += current
        if is_retirement:
            measured_retirement += balance
            estimated_retirement += current

    return LiveNetWorth(
        measured_on=newest,
        measured=measured,
        measured_retirement=measured_retirement,
        measured_liquid=measured - measured_retirement,
        estimated=estimated,
        estimated_retirement=estimated_retirement,
        estimated_liquid=estimated - estimated_retirement,
        priced_at=valuation.priced_at,
        marked_accounts=marked_count,
        carried_accounts=carried_count,
        warnings=valuation.warnings,
    )
