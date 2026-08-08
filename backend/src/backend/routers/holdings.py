"""Positions: preview first, commit second.

The same two-step shape as the CSV import, for the same reason — nothing is
written until someone has seen what it would write. What is being reviewed here
is different, though. A transaction import asks "is this the right category?".
This asks "is this the right account?", because a positions file identifies its
accounts by brokerage account number and budgeter has never seen those numbers
before. Answered once and remembered in `brokerage_accounts`, so the second
import is a drop and a click.

Committing a snapshot also records the account balance it implies, since the
file states it and typing a number that has already been parsed is how
transcription errors get in. It is a checkbox on the preview, not a silent side
effect.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import prices, valuation
from ..db import get_session
from ..fidelity_import import Statement, StatementAccount, parse_positions
from ..models import (
    Account,
    AccountBalance,
    BrokerageAccount,
    Holding,
    Security,
    SecurityKind,
)
from ..schemas import Money, Price, Quantity

router = APIRouter(prefix="/holdings", tags=["holdings"])

# Fidelity is the only brokerage this parser understands, and the export does
# not name itself. Kept as a parameter rather than a constant so the mapping
# table is already institution-scoped when a second brokerage turns up.
DEFAULT_INSTITUTION = "Fidelity"

# Words that carry no signal when matching a statement's account name against
# an account already on file. "Individual" should find "Individual Brokerage".
NOISE_WORDS = {"account", "the", "and", "of"}


class PositionIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    description: str = Field(min_length=1, max_length=120)
    quantity: Decimal | None = None
    price: Decimal | None = None
    value: Decimal
    cost_basis: Decimal | None = None
    kind: SecurityKind


class PositionOut(BaseModel):
    symbol: str
    description: str
    quantity: Quantity | None
    price: Price | None
    value: Money
    cost_basis: Money | None
    kind: SecurityKind
    # True when this security has never been seen before, so the import is
    # about to define what it is and how to price it.
    is_new: bool
    notes: list[str]


class PreviewAccount(BaseModel):
    external_number: str
    external_name: str
    total_value: Money
    total_cost_basis: Money | None
    positions: list[PositionOut]

    # Which budgeter account these positions belong to. Already answered if
    # this number has been imported before; otherwise a guess from the name,
    # which the reviewer confirms or changes.
    account_id: int | None
    account_label: str | None
    is_remembered: bool
    match_note: str | None


class PreviewOut(BaseModel):
    as_of: date | None
    institution: str
    accounts: list[PreviewAccount]
    errors: list[str]
    # True when positions for this date are already on file. Committing
    # replaces them, which is right for a corrected download and worth saying.
    replaces_existing: bool


class CommitAccountIn(BaseModel):
    external_number: str = Field(min_length=1, max_length=40)
    external_name: str | None = Field(default=None, max_length=60)
    account_id: int
    positions: list[PositionIn]


class CommitIn(BaseModel):
    as_of: date
    institution: str = Field(default=DEFAULT_INSTITUTION, max_length=60)
    accounts: list[CommitAccountIn]
    # The file states what each account is worth, so the balance snapshot is
    # free. Off is allowed; silently skipping it would not be.
    record_balances: bool = True


class CommitOut(BaseModel):
    accounts: int
    positions: int
    securities_created: int
    balances_recorded: int
    # Securities that came in with no way to price them. The screen turns these
    # into a prompt, because an unanswered one silently freezes an account.
    needs_proxy: list[str]
    warnings: list[str]


def _normalise(name: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in name.lower())
    return {w for w in cleaned.split() if w and w not in NOISE_WORDS}


def _suggest_account(
    session: Session, statement_account: StatementAccount, institution: str
) -> tuple[int | None, str | None, str | None, bool]:
    """Find the budgeter account these positions belong to.

    Returns (account_id, label, note, remembered). A remembered mapping is
    authoritative; a name match is only ever a suggestion, and an ambiguous one
    is no suggestion at all — pointing a $124,000 retirement snapshot at the
    wrong account is not a mistake worth being clever about.
    """
    mapping = session.scalar(
        select(BrokerageAccount).where(
            BrokerageAccount.institution == institution,
            BrokerageAccount.external_number == statement_account.external_number,
        )
    )
    if mapping is not None:
        account = session.get(Account, mapping.account_id)
        label = f"{account.institution} · {account.name}" if account else None
        return mapping.account_id, label, "mapped on a previous import", True

    wanted = _normalise(statement_account.external_name)
    candidates = session.scalars(
        select(Account).where(
            Account.institution == institution, Account.closed_on.is_(None)
        )
    ).all()

    scored = sorted(
        ((len(wanted & _normalise(a.name)), a) for a in candidates),
        key=lambda pair: -pair[0],
    )
    if not scored or scored[0][0] == 0:
        return None, None, "no account matched — choose one", False
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, None, "more than one account matched — choose one", False

    best = scored[0][1]
    return (
        best.id,
        f"{best.institution} · {best.name}",
        f"matched on the name “{statement_account.external_name}”",
        False,
    )


def _to_preview(
    session: Session, statement: Statement, institution: str
) -> PreviewOut:
    known = set(
        session.scalars(
            select(Security.symbol).where(
                Security.symbol.in_(
                    {p.symbol for a in statement.accounts for p in a.positions}
                )
            )
        )
    )

    accounts = []
    for account in statement.accounts:
        account_id, label, note, remembered = _suggest_account(
            session, account, institution
        )
        accounts.append(
            PreviewAccount(
                external_number=account.external_number,
                external_name=account.external_name,
                total_value=account.total_value,
                total_cost_basis=account.total_cost_basis,
                account_id=account_id,
                account_label=label,
                is_remembered=remembered,
                match_note=note,
                positions=[
                    PositionOut(
                        symbol=p.symbol,
                        description=p.description,
                        quantity=p.quantity,
                        price=p.price,
                        value=p.value,
                        cost_basis=p.cost_basis,
                        kind=p.kind,
                        is_new=p.symbol not in known,
                        notes=p.notes,
                    )
                    for p in account.positions
                ],
            )
        )

    replaces = False
    if statement.as_of is not None:
        replaces = bool(
            session.scalar(
                select(func.count(Holding.id)).where(Holding.as_of == statement.as_of)
            )
        )

    return PreviewOut(
        as_of=statement.as_of,
        institution=institution,
        accounts=accounts,
        errors=statement.errors,
        replaces_existing=replaces,
    )


@router.post("/preview", response_model=PreviewOut)
async def preview(
    file: UploadFile = File(...),
    institution: str = Form(DEFAULT_INSTITUTION),
    session: Session = Depends(get_session),
):
    """Read a positions export and show what committing it would do."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "that file is empty")
    return _to_preview(session, parse_positions(content), institution.strip())


@router.post("/commit", response_model=CommitOut)
def commit(payload: CommitIn, session: Session = Depends(get_session)):
    """Write the snapshot: mappings, securities, positions, balances."""
    if not payload.accounts:
        raise HTTPException(400, "nothing to import")

    institution = payload.institution.strip() or DEFAULT_INSTITUTION
    created_securities: list[str] = []
    needs_proxy: list[str] = []
    positions_written = 0
    balances_written = 0

    for entry in payload.accounts:
        account = session.get(Account, entry.account_id)
        if account is None:
            raise HTTPException(404, f"account {entry.account_id} not found")

        # Remember the mapping so no future export has to be pointed by hand.
        # Keyed on the account number, which is what the brokerage guarantees;
        # the display name is stored only to make the next preview readable.
        mapping = session.scalar(
            select(BrokerageAccount).where(
                BrokerageAccount.institution == institution,
                BrokerageAccount.external_number == entry.external_number,
            )
        )
        if mapping is None:
            session.add(
                BrokerageAccount(
                    institution=institution,
                    external_number=entry.external_number,
                    external_name=entry.external_name,
                    account_id=entry.account_id,
                )
            )
        else:
            mapping.account_id = entry.account_id
            mapping.external_name = entry.external_name or mapping.external_name

        for position in entry.positions:
            security = session.get(Security, position.symbol)
            if security is None:
                security = Security(
                    symbol=position.symbol,
                    description=position.description[:120],
                    kind=position.kind,
                    # A security quotes under its own name unless it cannot
                    # quote at all. The unquotable case is left null rather
                    # than guessed: a wrong stand-in is worse than a missing
                    # one, because it produces a plausible number.
                    quote_symbol=(
                        None
                        if position.kind
                        in (SecurityKind.UNQUOTED, SecurityKind.MONEY_MARKET)
                        else position.symbol
                    ),
                )
                session.add(security)
                created_securities.append(position.symbol)
            # An existing security keeps its kind and its proxy. Both may have
            # been corrected by hand, and re-importing must not undo that.

            if (
                security.kind is SecurityKind.UNQUOTED
                and not security.quote_symbol
                and position.symbol not in needs_proxy
            ):
                needs_proxy.append(position.symbol)

        # Replacing rather than merging: a snapshot for a date is one reading,
        # and a corrected re-download must not leave a sold position behind.
        session.execute(
            delete(Holding).where(
                Holding.account_id == entry.account_id,
                Holding.as_of == payload.as_of,
            )
        )
        session.flush()

        for position in entry.positions:
            session.add(
                Holding(
                    account_id=entry.account_id,
                    as_of=payload.as_of,
                    symbol=position.symbol,
                    quantity=position.quantity,
                    price=position.price,
                    value=position.value,
                    cost_basis=position.cost_basis,
                )
            )
            positions_written += 1

        if payload.record_balances:
            total = sum((p.value for p in entry.positions), Decimal(0))
            existing = session.scalar(
                select(AccountBalance).where(
                    AccountBalance.account_id == entry.account_id,
                    AccountBalance.as_of == payload.as_of,
                )
            )
            if existing is None:
                session.add(
                    AccountBalance(
                        account_id=entry.account_id,
                        as_of=payload.as_of,
                        balance=total,
                    )
                )
            else:
                existing.balance = total
            balances_written += 1

    session.commit()

    # Now that the securities exist, find out what they actually are and get a
    # first price. Best-effort by design — an import must not fail because a
    # quote endpoint is down, and the positions are already safely on file.
    warnings: list[str] = []
    try:
        kinds, errors = prices.refresh(session, valuation.symbols_to_refresh(session))
        warnings.extend(errors)
        for symbol, kind in kinds.items():
            security = session.get(Security, symbol)
            # Only ever refines a guess. The two kinds the parser is certain
            # about are exactly the two a provider is worst at.
            if security is not None and security.kind not in (
                SecurityKind.UNQUOTED,
                SecurityKind.MONEY_MARKET,
            ):
                security.kind = kind
        session.commit()
    except Exception as exc:  # noqa: BLE001 — a price is not worth losing the import over
        warnings.append(f"prices could not be fetched: {exc}")

    return CommitOut(
        accounts=len(payload.accounts),
        positions=positions_written,
        securities_created=len(created_securities),
        balances_recorded=balances_written,
        needs_proxy=needs_proxy,
        warnings=warnings,
    )


class HoldingOut(BaseModel):
    symbol: str
    description: str
    kind: SecurityKind
    quantity: Quantity | None
    cost_basis: Money | None
    statement_value: Money
    statement_price: Price | None
    value: Money
    price: Price | None
    change: Money
    gain: Money | None
    basis: valuation.PricingBasis
    priced_on: date | None
    proxy_symbol: str | None
    note: str | None


class HoldingAccountOut(BaseModel):
    account_id: int
    institution: str
    name: str
    is_retirement: bool
    as_of: date
    statement_value: Money
    value: Money
    cost_basis: Money | None
    gain: Money | None
    is_estimated: bool
    holdings: list[HoldingOut]


class PortfolioOut(BaseModel):
    accounts: list[HoldingAccountOut]
    statement_value: Money
    value: Money
    cost_basis: Money | None
    # Value minus what was paid: the part of the balance the market provided
    # rather than the budget. Null unless every position reports a cost basis.
    gain: Money | None
    priced_at: str | None
    warnings: list[str]
    # Symbols with no way to be priced, so the screen can ask for a stand-in
    # instead of quietly showing a stale figure.
    needs_proxy: list[str]


def _portfolio(result: valuation.Valuation, needs_proxy: list[str]) -> PortfolioOut:
    return PortfolioOut(
        accounts=[
            HoldingAccountOut(
                account_id=a.account_id,
                institution=a.institution,
                name=a.name,
                is_retirement=a.is_retirement,
                as_of=a.as_of,
                statement_value=a.statement_value,
                value=a.value,
                cost_basis=a.cost_basis,
                gain=a.gain,
                is_estimated=a.is_estimated,
                holdings=[
                    HoldingOut(
                        symbol=h.symbol,
                        description=h.description,
                        kind=h.kind,
                        quantity=h.quantity,
                        cost_basis=h.cost_basis,
                        statement_value=h.statement_value,
                        statement_price=h.statement_price,
                        value=h.value,
                        price=h.price,
                        change=h.change,
                        gain=h.gain,
                        basis=h.basis,
                        priced_on=h.priced_on,
                        proxy_symbol=h.proxy_symbol,
                        note=h.note,
                    )
                    for h in a.holdings
                ],
            )
            for a in result.accounts
        ],
        statement_value=result.statement_value,
        value=result.value,
        cost_basis=result.cost_basis,
        gain=(
            None
            if result.cost_basis is None
            else result.value - result.cost_basis
        ),
        priced_at=result.priced_at.isoformat() if result.priced_at else None,
        warnings=result.warnings,
        needs_proxy=needs_proxy,
    )


def _unpriceable(session: Session) -> list[str]:
    """Held securities with no way to reach a price."""
    dates = valuation.latest_holding_dates(session)
    if not dates:
        return []
    rows = session.execute(
        select(Security.symbol)
        .join(Holding, Holding.symbol == Security.symbol)
        .where(
            Security.quote_symbol.is_(None),
            Security.kind != SecurityKind.MONEY_MARKET,
            Holding.account_id.in_(dates.keys()),
        )
        .distinct()
    ).all()
    return [symbol for (symbol,) in rows]


@router.get("", response_model=PortfolioOut)
def portfolio(refresh: bool = True, session: Session = Depends(get_session)):
    """Every position, valued now. `refresh=false` values from the cache."""
    result = valuation.value_portfolio(session, refresh=refresh)
    return _portfolio(result, _unpriceable(session))


class SecurityOut(BaseModel):
    symbol: str
    description: str
    kind: SecurityKind
    quote_symbol: str | None
    is_held: bool


class SecurityPatch(BaseModel):
    # An empty string clears the stand-in and returns the holding to being
    # carried at its statement value. Omitted means "leave it alone".
    quote_symbol: str | None = Field(default=None, max_length=20)
    kind: SecurityKind | None = None


@router.get("/securities", response_model=list[SecurityOut])
def list_securities(session: Session = Depends(get_session)):
    dates = valuation.latest_holding_dates(session)
    held = set(
        session.scalars(
            select(Holding.symbol).where(Holding.account_id.in_(dates.keys() or [0]))
        )
    )
    rows = session.scalars(select(Security).order_by(Security.symbol)).all()
    return [
        SecurityOut(
            symbol=s.symbol,
            description=s.description,
            kind=s.kind,
            quote_symbol=s.quote_symbol,
            is_held=s.symbol in held,
        )
        for s in rows
    ]


@router.patch("/securities/{symbol}", response_model=SecurityOut)
def update_security(
    symbol: str, payload: SecurityPatch, session: Session = Depends(get_session)
):
    """Set what a security is, or what stands in for it.

    Setting a stand-in is checked against the provider before it is saved. A
    typo'd ticker would otherwise be accepted and then quietly fail to price,
    which looks exactly like the problem it was meant to fix.
    """
    security = session.get(Security, symbol.upper())
    if security is None:
        raise HTTPException(404, f"{symbol} is not on file")

    data = payload.model_dump(exclude_unset=True)

    if "quote_symbol" in data:
        quote = (data["quote_symbol"] or "").strip().upper()
        if not quote:
            security.quote_symbol = None
        else:
            series = prices.fetch_series(quote)
            if series.error or series.latest is None:
                raise HTTPException(
                    400, series.error or f"no price available for {quote}"
                )
            security.quote_symbol = quote
            prices.refresh(session, {quote: _earliest_holding(session, security.symbol)})

    if data.get("kind") is not None:
        security.kind = data["kind"]

    session.commit()
    return SecurityOut(
        symbol=security.symbol,
        description=security.description,
        kind=security.kind,
        quote_symbol=security.quote_symbol,
        is_held=True,
    )


def _earliest_holding(session: Session, symbol: str) -> date | None:
    """How far back a stand-in has to reach for this security.

    A proxy is useless without a price on the snapshot date, so setting one
    backfills history rather than only today.
    """
    return session.scalar(select(func.min(Holding.as_of)).where(Holding.symbol == symbol))
