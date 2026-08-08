"""Parsing a Fidelity 'Portfolio Positions' export.

A different kind of file from the ones `csv_import` handles. That module reads
transactions — things that happened. This one reads *positions*: what is held
right now, which is a photograph rather than a diary. Nothing here becomes a
transaction, and none of it is spending.

Fidelity's export is a CSV with a tail of prose on the end: two paragraphs of
legal disclaimer and a download timestamp, all inside the same column grid, and
all of it arriving after a blank row. Reading to end-of-file and hoping is how
a disclaimer becomes a holding worth $0, so the blank row is treated as the end
of the data and the tail is mined only for the date.

Fidelity is the reason this exists at all. It shares data through Akoya, which
has no individual developer access, so unlike every other institution here it
can never be linked over Plaid. Dropping this file *is* the refresh.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import SecurityKind

# The export's own column names. Fidelity has kept these stable, but a rename
# would otherwise fail as a file full of empty rows, so they are looked up by
# name and a missing one is reported rather than skipped past.
REQUIRED_HEADERS = ("Account number", "Symbol", "Current value")

# 'Date downloaded Aug-08-2026 12:06 p.m ET', on its own line after the
# disclaimers. This is the date the positions were true — not today, which may
# be days later by the time the file is dropped.
DOWNLOADED = re.compile(r"Date downloaded\s+([A-Za-z]{3}-\d{1,2}-\d{4})")

# What Fidelity puts in Description when the line is uninvested cash.
MONEY_MARKET = "HELD IN MONEY MARKET"

# Everything that is not part of a number. Values arrive as '$2,114.17 ' with a
# trailing space, and negatives as '($45.58)' rather than with a minus sign.
NOT_NUMERIC = re.compile(r"[^0-9.\-]")

# A CUSIP where a ticker should be. Nine characters, ending in a check digit —
# which is what Fidelity falls back to for something that has no ticker because
# it does not trade publicly. On this data it is 31564E540, the Moody's plan
# trust, and it is 76% of the portfolio.
CUSIP = re.compile(r"^[A-Z0-9]{8}\d$")


@dataclass
class PositionRow:
    """One position line, as the statement stated it."""

    row_number: int
    external_number: str
    external_name: str
    symbol: str
    description: str
    quantity: Decimal | None
    price: Decimal | None
    value: Decimal
    cost_basis: Decimal | None
    # A first guess, refined by whatever the price provider says the symbol
    # actually is. Never guessed at all for the two that matter: a money-market
    # line says so in its description, and a CUSIP is unquotable by definition.
    kind: SecurityKind
    notes: list[str] = field(default_factory=list)

    @property
    def gain(self) -> Decimal | None:
        return None if self.cost_basis is None else self.value - self.cost_basis


@dataclass
class StatementAccount:
    """One account on the statement, and everything held in it."""

    external_number: str
    external_name: str
    positions: list[PositionRow] = field(default_factory=list)

    @property
    def total_value(self) -> Decimal:
        """What the account is worth, summed from its own positions.

        Fidelity does not print an account total in this export, so this is the
        figure that becomes the balance snapshot. Summing the parts is also the
        only available check that the file was read correctly.
        """
        return sum((p.value for p in self.positions), Decimal(0))

    @property
    def total_cost_basis(self) -> Decimal | None:
        """Null unless every position reports one — a partial sum is a lie.

        Money-market lines carry no cost basis, so an account holding cash
        alongside funds would otherwise report a growth figure computed against
        a total that excludes the cash.
        """
        if any(p.cost_basis is None for p in self.positions):
            return None
        return sum((p.cost_basis for p in self.positions), Decimal(0))


@dataclass
class Statement:
    as_of: date | None
    accounts: list[StatementAccount] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_money(raw: str | None) -> Decimal | None:
    """'$2,114.17 ' -> 2114.17, '($45.58)' -> -45.58, '' -> None."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or text in {"--", "n/a", "N/A"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = NOT_NUMERIC.sub("", text)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def clean_symbol(raw: str) -> str:
    """Strip Fidelity's footnote asterisks: 'SPAXX**' is SPAXX."""
    return raw.strip().rstrip("*").strip().upper()


def classify(symbol: str, description: str) -> SecurityKind:
    """A first guess at what this is, from the two columns that can tell.

    Deliberately conservative. The provider lookup at import time knows the
    real answer for anything with a ticker, and the only two cases that must be
    right without a network are the two this can be certain about: cash held in
    a money market, and a CUSIP that will never have a quote.
    """
    if MONEY_MARKET in description.upper():
        return SecurityKind.MONEY_MARKET
    if CUSIP.match(symbol):
        return SecurityKind.UNQUOTED
    # Five letters ending in X is the mutual-fund convention and holds for
    # every fund in this file. Anything shorter is an ETF or a share, which
    # price identically, so guessing between them costs nothing.
    if len(symbol) == 5 and symbol.endswith("X"):
        return SecurityKind.MUTUAL_FUND
    return SecurityKind.EQUITY


def _find_as_of(text: str) -> date | None:
    match = DOWNLOADED.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%b-%d-%Y").date()
    except ValueError:
        return None


def parse_positions(content: bytes | str) -> Statement:
    """Read an export into accounts and positions.

    Rows are grouped by account number rather than returned flat, because
    everything downstream is per-account: the mapping to a budgeter account,
    the balance snapshot, and the valuation.
    """
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    # A BOM survives decoding when the caller passed a str that was read
    # elsewhere. Cheap to strip twice, expensive to miss — it lands on the
    # first header name and makes 'Account number' unfindable.
    text = text.lstrip("﻿")

    statement = Statement(as_of=_find_as_of(text))
    reader = csv.reader(io.StringIO(text))

    try:
        header = next(reader)
    except StopIteration:
        statement.errors.append("the file is empty")
        return statement

    columns = {name.strip().lstrip("﻿"): i for i, name in enumerate(header)}
    missing = [h for h in REQUIRED_HEADERS if h not in columns]
    if missing:
        statement.errors.append(
            "this does not look like a Fidelity positions export — "
            f"no {', '.join(missing)} column"
        )
        return statement

    def cell(row: list[str], name: str) -> str:
        index = columns.get(name)
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    by_number: dict[str, StatementAccount] = {}

    for row_number, row in enumerate(reader, start=2):
        # The blank row is the end of the data. Everything after it is the
        # disclaimer, which is real text in a real cell and would otherwise
        # parse as a position with no symbol and no value.
        if not any(c.strip() for c in row):
            break

        number = cell(row, "Account number")
        symbol = clean_symbol(cell(row, "Symbol"))
        description = cell(row, "Description")
        value = parse_money(cell(row, "Current value"))

        if not number:
            continue
        if not symbol:
            # 'Pending activity' shows up this way — a real dollar figure with
            # nothing held. Reported rather than dropped, because it is money
            # that belongs to the account and will not be in the total.
            if value:
                statement.errors.append(
                    f"row {row_number}: {description or 'a line'} in account "
                    f"{number} has no symbol and is not counted ({value})"
                )
            continue
        if value is None:
            statement.errors.append(
                f"row {row_number}: {symbol} has no current value and is skipped"
            )
            continue

        account = by_number.get(number)
        if account is None:
            account = StatementAccount(
                external_number=number,
                external_name=cell(row, "Account name") or number,
            )
            by_number[number] = account

        position = PositionRow(
            row_number=row_number,
            external_number=number,
            external_name=account.external_name,
            symbol=symbol,
            description=description or symbol,
            quantity=parse_money(cell(row, "Quantity")),
            price=parse_money(cell(row, "Last price")),
            value=value,
            cost_basis=parse_money(cell(row, "Cost basis total")),
            kind=classify(symbol, description),
        )

        if position.kind is SecurityKind.UNQUOTED:
            position.notes.append(
                "no public quote — this is a plan-only fund identified by CUSIP"
            )
        if position.quantity is None and position.kind is not SecurityKind.MONEY_MARKET:
            position.notes.append("no quantity on the statement")

        account.positions.append(position)

    if not by_number and not statement.errors:
        statement.errors.append("no positions found in the file")
    if statement.as_of is None and by_number:
        statement.errors.append(
            "no 'Date downloaded' line — say what date these positions are from"
        )

    statement.accounts = list(by_number.values())
    return statement
