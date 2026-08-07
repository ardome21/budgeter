"""Read-only derived views: categories, periods, the month summary, overview."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import queries
from ..db import get_session
from ..models import Category
from ..schemas import CategoryOut, MonthSummary, OverviewOut, PeriodOut

router = APIRouter(tags=["views"])


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(session: Session = Depends(get_session)):
    return session.scalars(
        select(Category).order_by(Category.sort_order, Category.name)
    ).all()


@router.get("/periods", response_model=list[PeriodOut])
def list_periods(session: Session = Depends(get_session)):
    """Every month with data, newest first. The tab strip, without the tabs."""
    return queries.list_periods(session)


@router.get("/periods/{year}/{month}/summary", response_model=MonthSummary)
def month_summary(year: int, month: int, session: Session = Depends(get_session)):
    if not 1 <= month <= 12:
        raise HTTPException(422, "month must be between 1 and 12")
    summary = queries.month_summary(session, year, month)
    if summary is None:
        raise HTTPException(404, f"no data for {year}-{month:02d}")
    return summary


@router.get("/overview", response_model=OverviewOut)
def overview(session: Session = Depends(get_session)):
    return queries.overview(session)
