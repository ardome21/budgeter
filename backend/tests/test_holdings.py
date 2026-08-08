"""Positions, prices and what the portfolio is worth.

Nothing here touches the network. `prices.fetch_series` is the single seam the
provider sits behind, and every test that needs a quote replaces it — so the
suite is as green on a plane as it is online, and a Yahoo outage is never a
build failure.

The load-bearing test in this file is `test_estimates_are_never_written`. The
whole design rests on marked-to-market figures being derived on read, and that
is exactly the kind of rule that a later convenience quietly breaks.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend import prices, valuation
from backend.fidelity_import import classify, parse_money, parse_positions
from backend.models import (
    Account,
    AccountBalance,
    BrokerageAccount,
    Holding,
    Security,
    SecurityKind,
    SecurityPrice,
)

# A faithful miniature of the real export: the BOM, the trailing space inside
# every dollar figure, a parenthesised loss, a money-market line with no
# quantity, a CUSIP where a ticker should be, and the disclaimer tail that
# arrives after a blank row and must not be read as holdings.
EXPORT = (
    "﻿Account number,Account name,Symbol,Description,Quantity,Last price,"
    "Last price change,Current value,Today's gain/loss dollar,"
    "Today's gain/loss percent,Total gain/loss dollar,Total gain/loss percent,"
    "Percent of account,Cost basis total,Average cost basis,Type\n"
    'Z1,Individual,VT,VANGUARD TOT WORLD,35.502,$161.30 ,$1.38 ,"$5,726.47 ",'
    '$48.99 ,0.86%,$43.48 ,0.76%,100.00%,"$5,682.99 ",$160.08 ,Cash\n'
    "Z1,Individual,FIGB,FIDELITY INV GRADE BOND ETF,78.579,$42.37 ,$0.11 ,"
    '"$3,329.39 ",$8.25 ,0.24%,($45.58),-1.36%,14.04%,"$3,374.97 ",$42.95 ,Cash\n'
    "R2,ROTH IRA,SPAXX**,HELD IN MONEY MARKET,,,,$12.50 ,,,,,0.00%,,,Cash\n"
    "P3,MOODY'S PPP,31564E540,FID FRDM INX 2065 T,5460.762,$22.70 ,$0.18 ,"
    '"$123,959.29 ",$982.94 ,0.80%,"$36,306.25 ",41.42%,100.00%,'
    '"$87,653.04 ",$16.05 ,\n'
    ",,,,,,,,,,,,,,,\n"
    '"The data and information in this spreadsheet is provided to you solely '
    'for your use and is not for distribution.",,,,,,,,,,,,,,,\n'
    ",,,,,,,,,,,,,,,\n"
    "Date downloaded Aug-08-2026 12:06 p.m ET,,,,,,,,,,,,,,,"
)

AS_OF = date(2026, 8, 8)

# Net worth is defined over *the most recent snapshot date in the database*, and
# these tests run against the real development database inside a transaction
# that is rolled back. Dating their snapshots far in the future is what keeps
# `max(as_of)` theirs alone — otherwise the developer's own accounts report on
# the same day and the totals include them. Same class of problem the `client`
# fixture solves for users and linked banks, solved without touching a row.
ALONE = date(2099, 1, 15)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$2,114.17 ", Decimal("2114.17")),
        ("($45.58)", Decimal("-45.58")),
        ("$0.02 ", Decimal("0.02")),
        ("5460.762", Decimal("5460.762")),
        ("", None),
        ("--", None),
        (None, None),
    ],
)
def test_money_shapes(raw, expected):
    assert parse_money(raw) == expected


def test_classify_is_certain_about_the_two_that_matter():
    # A CUSIP can never be quoted, and cash in a money market is a dollar.
    # Everything else is a guess the provider is allowed to overrule.
    assert classify("31564E540", "FID FRDM INX 2065 T") is SecurityKind.UNQUOTED
    assert classify("SPAXX", "HELD IN MONEY MARKET") is SecurityKind.MONEY_MARKET
    assert classify("FLOWX", "FIDELITY WATER FUND") is SecurityKind.MUTUAL_FUND
    assert classify("MCO", "MOODYS CORP") is SecurityKind.EQUITY


def test_parses_the_export():
    statement = parse_positions(EXPORT.encode("utf-8"))

    assert statement.errors == []
    assert statement.as_of == AS_OF
    assert [a.external_number for a in statement.accounts] == ["Z1", "R2", "P3"]

    individual = statement.accounts[0]
    assert individual.total_value == Decimal("9055.86")
    assert individual.total_cost_basis == Decimal("9057.96")

    vt = individual.positions[0]
    assert vt.quantity == Decimal("35.502")
    assert vt.price == Decimal("161.30")
    assert vt.value == Decimal("5726.47")

    # A money-market line has a value and no unit count at all.
    cash = statement.accounts[1].positions[0]
    assert cash.symbol == "SPAXX"  # the footnote asterisks are not part of it
    assert cash.quantity is None
    assert cash.kind is SecurityKind.MONEY_MARKET

    # Cost basis is missing on the cash line, so the account reports none
    # rather than a total that silently excludes it.
    assert statement.accounts[1].total_cost_basis is None


def test_the_disclaimer_is_not_a_holding():
    """The prose after the blank row is inside the same column grid."""
    statement = parse_positions(EXPORT)
    symbols = {p.symbol for a in statement.accounts for p in a.positions}
    assert symbols == {"VT", "FIGB", "SPAXX", "31564E540"}


def test_a_file_from_somewhere_else_is_refused():
    statement = parse_positions("Date,Description,Amount\n2026-01-01,Coffee,4.50\n")
    assert statement.accounts == []
    assert "does not look like a Fidelity positions export" in statement.errors[0]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _only_our_positions(session: Session):
    """Clear real positions and cached prices for the duration of each test.

    Same reasoning as the user and linked-bank cleanup in conftest, and found
    the same way: importing a real statement onto this machine turned twelve of
    these red at once. Valuation reads *every* account holding something, so the
    developer's own Fidelity accounts sorted ahead of the fixture's and
    `accounts[0]` stopped being the account the test created. The cached prices
    bite harder and quieter — a real quote for VT dated today is fresh, so the
    fake provider is never called and the test's price silently does nothing.

    Inside the transaction, so the rollback puts all of it back.
    """
    session.execute(delete(Holding))
    session.execute(delete(BrokerageAccount))
    session.execute(delete(Security))
    session.execute(delete(SecurityPrice))
    session.flush()


@pytest.fixture
def brokerage(session: Session) -> Account:
    account = Account(
        institution="Test Brokerage", name="Individual Brokerage", is_retirement=False
    )
    session.add(account)
    session.flush()
    return account


def _fake_series(quotes: dict[str, list[tuple[date, str]]]):
    """A stand-in provider. `quotes` maps symbol -> [(date, close)]."""

    def fetch(symbol: str, since: date | None = None) -> prices.Series:
        points = quotes.get(symbol)
        if not points:
            return prices.Series(symbol=symbol, error=f"{symbol}: no data found")
        closes = [(d, Decimal(c)) for d, c in points]
        return prices.Series(
            symbol=symbol,
            closes=closes,
            latest=closes[-1][1],
            latest_at=datetime.combine(closes[-1][0], datetime.min.time(), tzinfo=UTC),
            kind=SecurityKind.ETF,
        )

    return fetch


def _hold(
    session: Session,
    account: Account,
    symbol: str,
    *,
    kind: SecurityKind,
    quote_symbol: str | None,
    quantity: str | None,
    value: str,
    price: str | None = None,
    cost_basis: str | None = None,
    as_of: date = AS_OF,
) -> Holding:
    if session.get(Security, symbol) is None:
        session.add(
            Security(
                symbol=symbol,
                description=f"{symbol} test security",
                kind=kind,
                quote_symbol=quote_symbol,
            )
        )
    holding = Holding(
        account_id=account.id,
        as_of=as_of,
        symbol=symbol,
        quantity=Decimal(quantity) if quantity else None,
        price=Decimal(price) if price else None,
        value=Decimal(value),
        cost_basis=Decimal(cost_basis) if cost_basis else None,
    )
    session.add(holding)
    session.flush()
    return holding


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------


def test_a_price_falls_back_to_the_last_close_before_it(session: Session):
    """A statement downloaded on a Saturday has no bar of its own."""
    for day, close in ((date(2026, 8, 6), "10.00"), (date(2026, 8, 7), "11.00")):
        session.add(
            SecurityPrice(
                symbol="AAA",
                as_of=day,
                close=Decimal(close),
                fetched_at=datetime.now(UTC),
            )
        )
    session.flush()

    assert prices.price_on(session, "AAA", date(2026, 8, 8)) == (
        Decimal("11.00"),
        date(2026, 8, 7),
    )
    # Never forward: valuing Thursday with Friday's price would make a proxy
    # ratio quietly wrong in the direction of the market's next move.
    assert prices.price_on(session, "AAA", date(2026, 8, 6)) == (
        Decimal("10.00"),
        date(2026, 8, 6),
    )
    assert prices.price_on(session, "AAA", date(2026, 8, 5)) is None


def test_a_fresh_price_is_not_refetched(session: Session, monkeypatch):
    calls: list[str] = []

    def counting(symbol: str, since: date | None = None) -> prices.Series:
        calls.append(symbol)
        return _fake_series({"AAA": [(AS_OF, "10.00")]})(symbol, since)

    monkeypatch.setattr(prices, "fetch_series", counting)

    prices.refresh(session, {"AAA": None})
    prices.refresh(session, {"AAA": None})
    assert calls == ["AAA"]

    # Ageing the cache past the window brings the provider back.
    row = session.scalar(select(SecurityPrice).where(SecurityPrice.symbol == "AAA"))
    row.fetched_at = datetime.now(UTC) - prices.PRICE_TTL - timedelta(minutes=1)
    session.flush()
    prices.refresh(session, {"AAA": None})
    assert calls == ["AAA", "AAA"]


def test_a_provider_failure_is_reported_not_raised(session: Session, monkeypatch):
    monkeypatch.setattr(prices, "fetch_series", _fake_series({}))
    kinds, errors = prices.refresh(session, {"NOPE": None})
    assert kinds == {}
    assert errors == ["NOPE: no data found"]


# --------------------------------------------------------------------------
# Valuation
# --------------------------------------------------------------------------


def test_a_live_holding_is_quantity_times_the_quote(
    session: Session, brokerage: Account, monkeypatch
):
    _hold(
        session,
        brokerage,
        "VT",
        kind=SecurityKind.ETF,
        quote_symbol="VT",
        quantity="35.502",
        price="161.30",
        value="5726.47",
        cost_basis="5682.99",
    )
    monkeypatch.setattr(prices, "fetch_series", _fake_series({"VT": [(AS_OF, "170.00")]}))

    result = valuation.value_portfolio(session)
    holding = result.accounts[0].holdings[0]

    assert holding.basis is valuation.PricingBasis.LIVE
    assert holding.value == Decimal("6035.34")  # 35.502 x 170.00
    assert holding.statement_value == Decimal("5726.47")
    assert holding.change == Decimal("308.87")
    assert holding.gain == Decimal("352.35")


def test_a_money_market_is_a_dollar_and_is_never_fetched(
    session: Session, brokerage: Account, monkeypatch
):
    _hold(
        session,
        brokerage,
        "SPAXX",
        kind=SecurityKind.MONEY_MARKET,
        quote_symbol=None,
        quantity=None,
        value="7790.53",
    )

    def explode(symbol: str, since: date | None = None):
        raise AssertionError(f"a money market must never be fetched, asked for {symbol}")

    monkeypatch.setattr(prices, "fetch_series", explode)

    holding = valuation.value_portfolio(session).accounts[0].holdings[0]
    assert holding.basis is valuation.PricingBasis.PAR
    assert holding.value == Decimal("7790.53")


def test_cash_costs_what_it_is_worth(
    session: Session, brokerage: Account, monkeypatch
):
    """A money market's cost basis is its value, not an unknown.

    The statement omits the column because there is no gain to report. Reading
    that as "unknown" nulls the cost basis of every account holding any cash,
    and with it the growth figure for the whole portfolio — which on the real
    data meant two of four accounts and the headline number.

    The stored row keeps the statement's own null; only the valuation fills it.
    """
    holding = _hold(
        session,
        brokerage,
        "SPAXX",
        kind=SecurityKind.MONEY_MARKET,
        quote_symbol=None,
        quantity=None,
        value="7790.53",
    )
    monkeypatch.setattr(prices, "fetch_series", _fake_series({}))

    account = valuation.value_portfolio(session).accounts[0]
    assert account.cost_basis == Decimal("7790.53")
    assert account.gain == Decimal(0)

    session.expire_all()
    assert session.get(Holding, holding.id).cost_basis is None


def test_a_proxy_borrows_the_movement_and_never_the_price(
    session: Session, brokerage: Account, monkeypatch
):
    """The case the whole feature turns on.

    The trust is quoted at $22.70 a unit and its stand-in at $19.93. Multiplying
    5460.762 units by the proxy price gives $108,833 — $15,000 short of the
    truth on the very day the statement was downloaded. Applying the proxy's
    *change* instead gives back the statement figure exactly, which is the only
    defensible answer when the proxy has not moved.
    """
    _hold(
        session,
        brokerage,
        "31564E540",
        kind=SecurityKind.UNQUOTED,
        quote_symbol="FFIJX",
        quantity="5460.762",
        price="22.70",
        value="123959.29",
        cost_basis="87653.04",
    )
    monkeypatch.setattr(
        prices,
        "fetch_series",
        _fake_series({"FFIJX": [(AS_OF, "19.93")]}),
    )

    holding = valuation.value_portfolio(session).accounts[0].holdings[0]
    assert holding.basis is valuation.PricingBasis.PROXY
    assert holding.proxy_symbol == "FFIJX"
    # Proxy unmoved since the statement, so the statement figure stands.
    assert holding.value == Decimal("123959.29")

    # Now move the stand-in 10% and the trust moves with it — not to 5460.762
    # x the proxy's own price, which would be a different number entirely.
    # A *new* dated row, not an edit to the old one: the ratio needs a price on
    # the snapshot date and a price now, and overwriting the first leaves the
    # rule comparing a number with itself.
    session.add(
        SecurityPrice(
            symbol="FFIJX",
            as_of=AS_OF + timedelta(days=5),
            close=Decimal("21.923"),
            fetched_at=datetime.now(UTC),
        )
    )
    session.flush()

    holding = valuation.value_portfolio(session, refresh=False).accounts[0].holdings[0]
    assert holding.value == Decimal("136355.22")  # 123959.29 x 1.10


def test_a_proxy_with_no_price_on_the_snapshot_date_carries(
    session: Session, brokerage: Account, monkeypatch
):
    """Half a ratio is not a ratio. The statement figure stands, and says so."""
    _hold(
        session,
        brokerage,
        "31564E540",
        kind=SecurityKind.UNQUOTED,
        quote_symbol="FFIJX",
        quantity="5460.762",
        value="123959.29",
    )
    # Only a price from *after* the snapshot, which must never be used to
    # value it.
    monkeypatch.setattr(
        prices,
        "fetch_series",
        _fake_series({"FFIJX": [(AS_OF + timedelta(days=3), "25.00")]}),
    )

    holding = valuation.value_portfolio(session).accounts[0].holdings[0]
    assert holding.basis is valuation.PricingBasis.CARRIED
    assert holding.value == Decimal("123959.29")
    assert "stand-in" in holding.note


def test_an_unpriceable_holding_is_carried_and_labelled(
    session: Session, brokerage: Account, monkeypatch
):
    _hold(
        session,
        brokerage,
        "31564E540",
        kind=SecurityKind.UNQUOTED,
        quote_symbol=None,
        quantity="5460.762",
        value="123959.29",
    )
    monkeypatch.setattr(prices, "fetch_series", _fake_series({}))

    holding = valuation.value_portfolio(session).accounts[0].holdings[0]
    assert holding.basis is valuation.PricingBasis.CARRIED
    assert holding.value == Decimal("123959.29")
    assert "no public quote" in holding.note


def test_estimates_are_never_written(
    session: Session, brokerage: Account, monkeypatch
):
    """The rule the whole design rests on.

    Valuing the portfolio may fetch prices and must cache them. It must not
    touch `account_balances` or `holdings`, because those are the record of
    what was measured — the thing the net worth chart is drawn from.
    """
    session.add(
        AccountBalance(
            account_id=brokerage.id, as_of=ALONE, balance=Decimal("5726.47")
        )
    )
    _hold(
        session,
        brokerage,
        "VT",
        kind=SecurityKind.ETF,
        quote_symbol="VT",
        quantity="35.502",
        price="161.30",
        value="5726.47",
        as_of=ALONE,
    )
    monkeypatch.setattr(prices, "fetch_series", _fake_series({"VT": [(ALONE, "200.00")]}))

    live = valuation.live_net_worth(session)
    assert live.estimated > live.measured

    session.expire_all()
    balance = session.scalar(
        select(AccountBalance).where(AccountBalance.account_id == brokerage.id)
    )
    holding = session.scalar(select(Holding).where(Holding.account_id == brokerage.id))
    assert balance.balance == Decimal("5726.47")
    assert holding.value == Decimal("5726.47")


def test_live_net_worth_carries_what_it_cannot_price(
    session: Session, brokerage: Account, monkeypatch
):
    """A checking balance is not marked to market, and the count says so."""
    checking = Account(institution="Test Bank", name="Checking")
    session.add(checking)
    session.flush()
    session.add_all(
        [
            AccountBalance(
                account_id=brokerage.id, as_of=ALONE, balance=Decimal("5726.47")
            ),
            AccountBalance(
                account_id=checking.id, as_of=ALONE, balance=Decimal("2241.11")
            ),
        ]
    )
    _hold(
        session,
        brokerage,
        "VT",
        kind=SecurityKind.ETF,
        quote_symbol="VT",
        quantity="35.502",
        price="161.30",
        value="5726.47",
        as_of=ALONE,
    )
    monkeypatch.setattr(prices, "fetch_series", _fake_series({"VT": [(ALONE, "170.00")]}))

    live = valuation.live_net_worth(session)

    assert live.measured_on == ALONE
    assert live.measured == Decimal("7967.58")
    assert live.estimated == Decimal("8276.45")  # only the brokerage moved
    assert live.change == Decimal("308.87")
    assert live.marked_accounts == 1
    assert live.carried_accounts == 1


def test_only_the_newest_snapshot_is_valued(
    session: Session, brokerage: Account, monkeypatch
):
    """An old positions file must not be added to the current one."""
    _hold(
        session,
        brokerage,
        "VT",
        kind=SecurityKind.ETF,
        quote_symbol="VT",
        quantity="10",
        value="1000.00",
        as_of=AS_OF - timedelta(days=30),
    )
    _hold(
        session,
        brokerage,
        "VT",
        kind=SecurityKind.ETF,
        quote_symbol="VT",
        quantity="20",
        value="2000.00",
        as_of=AS_OF,
    )
    monkeypatch.setattr(prices, "fetch_series", _fake_series({"VT": [(AS_OF, "100.00")]}))

    result = valuation.value_portfolio(session)
    assert len(result.accounts) == 1
    assert len(result.accounts[0].holdings) == 1
    assert result.value == Decimal("2000.00")


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------


def _upload(client, content: str = EXPORT):
    return client.post(
        "/api/holdings/preview",
        files={"file": ("positions.csv", content, "text/csv")},
        data={"institution": "Test Brokerage"},
    )


def test_preview_matches_an_account_by_name(client, session: Session, brokerage):
    response = _upload(client)
    assert response.status_code == 200
    body = response.json()

    assert body["as_of"] == "2026-08-08"
    individual = next(a for a in body["accounts"] if a["external_number"] == "Z1")
    assert individual["account_id"] == brokerage.id
    assert "matched on the name" in individual["match_note"]
    assert individual["total_value"] == "9055.86"
    assert individual["positions"][0]["is_new"] is True

    # Nothing on the statement matches the other two, so they are left for the
    # reviewer rather than guessed at.
    roth = next(a for a in body["accounts"] if a["external_number"] == "R2")
    assert roth["account_id"] is None


def test_commit_writes_positions_the_mapping_and_the_balance(
    client, session: Session, brokerage, monkeypatch
):
    monkeypatch.setattr(prices, "fetch_series", _fake_series({"VT": [(AS_OF, "161.30")]}))
    preview = _upload(client).json()
    individual = next(a for a in preview["accounts"] if a["external_number"] == "Z1")

    response = client.post(
        "/api/holdings/commit",
        json={
            "as_of": preview["as_of"],
            "institution": "Test Brokerage",
            "record_balances": True,
            "accounts": [
                {
                    "external_number": "Z1",
                    "external_name": "Individual",
                    "account_id": brokerage.id,
                    "positions": [
                        {
                            "symbol": p["symbol"],
                            "description": p["description"],
                            "quantity": p["quantity"],
                            "price": p["price"],
                            "value": p["value"],
                            "cost_basis": p["cost_basis"],
                            "kind": p["kind"],
                        }
                        for p in individual["positions"]
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["positions"] == 2
    assert body["securities_created"] == 2
    assert body["balances_recorded"] == 1

    session.expire_all()
    holdings = session.scalars(
        select(Holding).where(Holding.account_id == brokerage.id)
    ).all()
    assert {h.symbol for h in holdings} == {"VT", "FIGB"}

    # The balance the file implies, recorded rather than retyped.
    balance = session.scalar(
        select(AccountBalance).where(
            AccountBalance.account_id == brokerage.id, AccountBalance.as_of == AS_OF
        )
    )
    assert balance.balance == Decimal("9055.86")

    # And the account number is remembered, so the next export needs no answer.
    mapping = session.scalar(
        select(BrokerageAccount).where(BrokerageAccount.external_number == "Z1")
    )
    assert mapping.account_id == brokerage.id

    assert _upload(client).json()["accounts"][0]["is_remembered"] is True


def test_recommitting_a_date_replaces_that_snapshot(
    client, session: Session, brokerage, monkeypatch
):
    """A corrected re-download must not leave a sold position behind."""
    monkeypatch.setattr(prices, "fetch_series", _fake_series({}))

    def commit(positions):
        return client.post(
            "/api/holdings/commit",
            json={
                "as_of": "2026-08-08",
                "institution": "Test Brokerage",
                "record_balances": False,
                "accounts": [
                    {
                        "external_number": "Z1",
                        "account_id": brokerage.id,
                        "positions": positions,
                    }
                ],
            },
        )

    sold = {
        "symbol": "OLD",
        "description": "SOMETHING SOLD",
        "quantity": "5",
        "price": "10",
        "value": "50.00",
        "cost_basis": "40.00",
        "kind": "EQUITY",
    }
    kept = {**sold, "symbol": "VT", "description": "VANGUARD TOT WORLD"}

    assert commit([sold, kept]).status_code == 200
    assert commit([kept]).status_code == 200

    session.expire_all()
    holdings = session.scalars(
        select(Holding).where(Holding.account_id == brokerage.id)
    ).all()
    assert {h.symbol for h in holdings} == {"VT"}


def test_setting_a_stand_in_is_checked_before_it_is_saved(
    client, session: Session, brokerage, monkeypatch
):
    _hold(
        session,
        brokerage,
        "31564E540",
        kind=SecurityKind.UNQUOTED,
        quote_symbol=None,
        quantity="5460.762",
        value="123959.29",
    )
    session.commit()
    monkeypatch.setattr(
        prices, "fetch_series", _fake_series({"FFIJX": [(AS_OF, "19.93")]})
    )

    # A typo is refused rather than saved and left to fail quietly later.
    bad = client.patch(
        "/api/holdings/securities/31564E540", json={"quote_symbol": "FFIJXX"}
    )
    assert bad.status_code == 400
    assert "no data found" in bad.json()["detail"]

    good = client.patch(
        "/api/holdings/securities/31564E540", json={"quote_symbol": "ffijx"}
    )
    assert good.status_code == 200
    assert good.json()["quote_symbol"] == "FFIJX"

    portfolio = client.get("/api/holdings?refresh=false").json()
    assert portfolio["accounts"][0]["holdings"][0]["basis"] == "PROXY"
    assert portfolio["needs_proxy"] == []


def test_the_portfolio_endpoint_flags_what_it_cannot_price(
    client, session: Session, brokerage, monkeypatch
):
    _hold(
        session,
        brokerage,
        "31564E540",
        kind=SecurityKind.UNQUOTED,
        quote_symbol=None,
        quantity="5460.762",
        value="123959.29",
        cost_basis="87653.04",
    )
    session.commit()
    monkeypatch.setattr(prices, "fetch_series", _fake_series({}))

    body = client.get("/api/holdings").json()
    assert body["needs_proxy"] == ["31564E540"]
    assert body["value"] == "123959.29"
    assert body["gain"] == "36306.25"
    assert body["accounts"][0]["is_estimated"] is False


def test_live_net_worth_reports_both_figures(
    client, session: Session, brokerage, monkeypatch
):
    session.add(
        AccountBalance(
            account_id=brokerage.id, as_of=ALONE, balance=Decimal("5726.47")
        )
    )
    _hold(
        session,
        brokerage,
        "VT",
        kind=SecurityKind.ETF,
        quote_symbol="VT",
        quantity="35.502",
        price="161.30",
        value="5726.47",
        as_of=ALONE,
    )
    session.commit()
    monkeypatch.setattr(prices, "fetch_series", _fake_series({"VT": [(ALONE, "170.00")]}))

    body = client.get("/api/accounts/net-worth/live").json()
    assert body["measured_on"] == "2099-01-15"
    assert body["measured"] == "5726.47"
    assert body["estimated"] == "6035.34"
    assert body["change"] == "308.87"
    assert body["is_estimated"] is True
