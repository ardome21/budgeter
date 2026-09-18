"""The three questions the app exists to answer."""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import queries
from ..db import get_session
from ..schemas import MonthOut, NetWorthOut, PortfolioOut

router = APIRouter(tags=["views"])


@router.get("/months", response_model=list[date])
def months(db: Session = Depends(get_session)) -> list[date]:
    """Which months have anything in them, newest first."""
    return queries.months_with_data(db)


@router.get("/month/{year}/{month}", response_model=MonthOut)
def month(year: int, month: int, db: Session = Depends(get_session)) -> MonthOut:
    return MonthOut(**queries.month_summary(db, date(year, month, 1)))


@router.get("/net-worth", response_model=NetWorthOut)
def net_worth(
    days: int = Query(365, ge=1, le=3650),
    db: Session = Depends(get_session),
) -> NetWorthOut:
    return NetWorthOut(**queries.net_worth(db, limit=days))


@router.get("/portfolio", response_model=PortfolioOut)
def portfolio(db: Session = Depends(get_session)) -> PortfolioOut:
    return PortfolioOut(**queries.portfolio(db))
