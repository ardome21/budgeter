"""Where you stand, and the pay stub that explains the paycheck."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_session
from ..models import PayLine, PayStub
from ..overview import overview, stub_totals
from ..schemas import OverviewOut, PayStubIn, PayStubOut

router = APIRouter(tags=["overview"])


@router.get("/overview", response_model=OverviewOut)
def get_overview(
    months: int = Query(12, ge=1, le=60), db: Session = Depends(get_session)
) -> dict:
    return overview(db, months=months)


def _out(stub: PayStub) -> dict:
    return {
        "effective_from": stub.effective_from,
        "periods_per_year": stub.periods_per_year,
        "gross": stub.gross,
        "lines": stub.lines,
        **stub_totals(stub),
    }


@router.get("/pay", response_model=list[PayStubOut])
def list_stubs(db: Session = Depends(get_session)) -> list[dict]:
    """Every stub, newest first. The first is the one in force now."""
    stubs = db.scalars(
        select(PayStub)
        .order_by(PayStub.effective_from.desc())
        .options(selectinload(PayStub.lines))
    )
    return [_out(s) for s in stubs]


@router.put("/pay/{year}/{month}", response_model=PayStubOut)
def set_stub(
    year: int, month: int, body: PayStubIn, db: Session = Depends(get_session)
) -> dict:
    """Replace the stub starting in this month. It holds for every later month
    until another one starts."""
    when = date(year, month, 1)
    if sum(line.amount for line in body.lines if line.kind != "EMPLOYER") > body.gross:
        raise HTTPException(422, "What comes out is more than the gross pay.")
    db.execute(delete(PayStub).where(PayStub.effective_from == when))
    stub = PayStub(
        effective_from=when,
        periods_per_year=body.periods_per_year,
        gross=body.gross,
        lines=[PayLine(**line.model_dump()) for line in body.lines],
    )
    db.add(stub)
    db.commit()
    db.refresh(stub)
    return _out(stub)


@router.delete("/pay/{year}/{month}", status_code=204)
def delete_stub(year: int, month: int, db: Session = Depends(get_session)) -> None:
    db.execute(delete(PayStub).where(PayStub.effective_from == date(year, month, 1)))
    db.commit()

