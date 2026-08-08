"""Public market prices, fetched once and cached.

Everything that touches the network lives here, the way `plaid_client` confines
Plaid. Above this module a price is a row in `security_prices` with a date on
it, so valuation is testable without a network and a provider outage degrades
the app to "the last price we saw" instead of breaking it.

The provider is Yahoo's chart endpoint: no key, and — the reason it wins — it
quotes mutual funds and money markets, not just listed shares. The alternatives
either need a key with a daily cap smaller than a page refresh, or cover ETFs
and nothing else. It is an undocumented endpoint, so it is behind `fetch_series`
and swapping it is one function.

Two rules hold no matter what the provider says:

- **A failure is never an exception.** A stale price is a fine answer. A
  request that 500s because a fund symbol changed is not.
- **Money markets are never fetched.** SPAXX is a dollar by construction, and a
  provider returning 0.9998 would put a wobble into a cash balance that has
  none.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import SecurityKind, SecurityPrice

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# The endpoint refuses a request with no user agent. Nothing here identifies
# the user; it is the client string, not a credential.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; budgeter/1.0)"}

TIMEOUT = httpx.Timeout(10.0)

# How long a fetched price stays fresh. Quotes are delayed by roughly this much
# anyway, so a shorter window would spend requests to learn the same number —
# and the figure it feeds is labelled an estimate either way.
PRICE_TTL = timedelta(minutes=15)

# A money market is worth its par value. Not a guess and not fetched.
PAR = Decimal("1.0000")

# What the provider calls each instrument, mapped to what this app calls it.
# Worth taking: it is the only source that can tell an ETF from an ordinary
# share, which the export cannot.
PROVIDER_KINDS = {
    "ETF": SecurityKind.ETF,
    "EQUITY": SecurityKind.EQUITY,
    "MUTUALFUND": SecurityKind.MUTUAL_FUND,
    "MONEYMARKET": SecurityKind.MONEY_MARKET,
    "INDEX": SecurityKind.EQUITY,
}


@dataclass
class Series:
    """Daily closes for one symbol, newest last."""

    symbol: str
    closes: list[tuple[date, Decimal]] = field(default_factory=list)
    # The live quote, which on a trading day is ahead of the last daily close.
    latest: Decimal | None = None
    latest_at: datetime | None = None
    kind: SecurityKind | None = None
    error: str | None = None


def _decimal(value: object) -> Decimal | None:
    """A price, rounded to the four decimals the column actually holds.

    The response is JSON, so every close arrives as a float and prints as
    19.329999923706055. Quantizing here rather than letting the database do it
    keeps the value that was compared, cached and returned identical to the one
    stored — a ratio computed from the unrounded figure and checked against the
    rounded one disagrees in the last cent for no visible reason.
    """
    if value is None:
        return None
    try:
        out = Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        return None
    return out if out > 0 else None


def _range_for(since: date | None) -> str:
    """The smallest window that still reaches back to the date we need.

    The proxy rule needs a close on the *snapshot* date, which may be months
    behind, and asking for five years of daily bars to price today's portfolio
    is rude to a free endpoint.
    """
    if since is None:
        return "5d"
    days = (date.today() - since).days
    for limit, window in ((5, "5d"), (28, "1mo"), (85, "3mo"), (350, "1y")):
        if days <= limit:
            return window
    return "5y"


def fetch_series(symbol: str, since: date | None = None) -> Series:
    """Ask the provider for one symbol. Never raises.

    The seam the tests replace — nothing in the suite reaches the network.
    """
    series = Series(symbol=symbol)
    try:
        response = httpx.get(
            CHART_URL.format(symbol=symbol),
            params={"interval": "1d", "range": _range_for(since)},
            headers=HEADERS,
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        # Deliberately not raise_for_status. An unknown symbol comes back as a
        # 404 whose *body* says "No data found, symbol may be delisted" — the
        # one message worth showing. Raising on the status throws it away and
        # leaves the user with "HTTPStatusError" to act on.
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        series.error = f"{symbol}: {type(exc).__name__}"
        return series

    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        detail = (chart.get("error") or {}).get("description") or "no data"
        series.error = f"{symbol}: {detail}"
        return series

    result = results[0]
    meta = result.get("meta") or {}
    series.kind = PROVIDER_KINDS.get(str(meta.get("instrumentType") or "").upper())
    series.latest = _decimal(meta.get("regularMarketPrice"))
    stamp = meta.get("regularMarketTime")
    if isinstance(stamp, int):
        series.latest_at = datetime.fromtimestamp(stamp, tz=UTC)

    currency = str(meta.get("currency") or "USD").upper()
    if currency != "USD":
        # Every account here is dollar-denominated. Storing a price in another
        # currency as though it were dollars is a silent, enormous error.
        series.error = f"{symbol}: quoted in {currency}, not USD"
        return series

    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    for stamp, close in zip(stamps, closes):
        value = _decimal(close)
        if value is None or not isinstance(stamp, int):
            continue
        series.closes.append(
            (datetime.fromtimestamp(stamp, tz=UTC).date(), value)
        )

    if series.latest is None and series.closes:
        series.latest = series.closes[-1][1]
    if series.latest is None:
        series.error = f"{symbol}: no price in the response"
    return series


def _store(session: Session, symbol: str, points: list[tuple[date, Decimal]]) -> None:
    """Upsert daily closes, one row per date."""
    if not points:
        return
    now = datetime.now(UTC)
    wanted = {d: c for d, c in points}
    existing = {
        row.as_of: row
        for row in session.scalars(
            select(SecurityPrice).where(
                SecurityPrice.symbol == symbol,
                SecurityPrice.as_of.in_(wanted.keys()),
            )
        )
    }
    for as_of, close in wanted.items():
        row = existing.get(as_of)
        if row is None:
            session.add(
                SecurityPrice(
                    symbol=symbol, as_of=as_of, close=close, fetched_at=now
                )
            )
        else:
            row.close = close
            row.fetched_at = now


def _is_fresh(session: Session, symbol: str) -> bool:
    newest = session.scalar(
        select(SecurityPrice.fetched_at)
        .where(SecurityPrice.symbol == symbol)
        .order_by(SecurityPrice.fetched_at.desc())
        .limit(1)
    )
    return newest is not None and datetime.now(UTC) - newest < PRICE_TTL


def refresh(
    session: Session,
    symbols: dict[str, date | None],
    *,
    force: bool = False,
) -> tuple[dict[str, SecurityKind], list[str]]:
    """Bring the cache up to date for these symbols.

    `symbols` maps each symbol to the earliest date a price is needed for, so
    a proxy that has to reach back to an old snapshot asks for history and
    everything else asks for a few days.

    Returns what the provider says each symbol *is* — the only place an ETF can
    be told from a share — and a list of what could not be fetched. Errors are
    returned, never raised: the caller shows the last known price and says it
    is old.
    """
    kinds: dict[str, SecurityKind] = {}
    errors: list[str] = []

    for symbol, since in symbols.items():
        if not symbol:
            continue
        if not force and _is_fresh(session, symbol):
            continue
        series = fetch_series(symbol, since)
        if series.error:
            errors.append(series.error)
        if series.kind is not None:
            kinds[symbol] = series.kind

        points = list(series.closes)
        if series.latest is not None:
            # Today's live quote is stored under today's date, overwriting the
            # daily bar if the market is still open. Without this the portfolio
            # is priced at yesterday's close all day.
            stamp = series.latest_at.date() if series.latest_at else date.today()
            points.append((stamp, series.latest))
        _store(session, symbol, points)

    session.flush()
    return kinds, errors


def price_on(session: Session, symbol: str, as_of: date) -> tuple[Decimal, date] | None:
    """The close for this symbol on `as_of`, or the last one before it.

    Falling back to the previous close is required, not a convenience: markets
    shut at weekends, and a statement downloaded on a Saturday has no bar of
    its own. Never looks forward — using a later price to value an earlier
    snapshot is how a proxy ratio quietly becomes 1.0.
    """
    row = session.execute(
        select(SecurityPrice.close, SecurityPrice.as_of)
        .where(SecurityPrice.symbol == symbol, SecurityPrice.as_of <= as_of)
        .order_by(SecurityPrice.as_of.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def latest_price(session: Session, symbol: str) -> tuple[Decimal, date] | None:
    """The newest close on file for this symbol, whatever its date."""
    row = session.execute(
        select(SecurityPrice.close, SecurityPrice.as_of)
        .where(SecurityPrice.symbol == symbol)
        .order_by(SecurityPrice.as_of.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None
