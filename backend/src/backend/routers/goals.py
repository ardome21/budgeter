"""Goals: what you are saving for, in order, and whether you are on track."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..goals import goal_plan
from ..models import Goal
from ..schemas import GoalIn, GoalPlan

router = APIRouter(tags=["goals"])


def _check(body: GoalIn) -> None:
    if (body.amount is None) == (body.months_of_spending is None):
        raise HTTPException(422, "Give an amount or a number of months, not both.")


@router.get("/goals", response_model=GoalPlan)
def plan(db: Session = Depends(get_session)) -> dict:
    return goal_plan(db)


@router.post("/goals", response_model=GoalPlan, status_code=201)
def add(body: GoalIn, db: Session = Depends(get_session)) -> dict:
    """Added at the end of the list: a new goal does not jump the queue."""
    _check(body)
    last = db.scalar(select(func.max(Goal.sort_order))) or 0
    db.add(Goal(**body.model_dump(), sort_order=last + 1))
    db.commit()
    return goal_plan(db)


@router.put("/goals/{goal_id}", response_model=GoalPlan)
def update(goal_id: int, body: GoalIn, db: Session = Depends(get_session)) -> dict:
    _check(body)
    goal = db.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(404, "No such goal.")
    for key, value in body.model_dump().items():
        setattr(goal, key, value)
    db.commit()
    return goal_plan(db)


@router.delete("/goals/{goal_id}", response_model=GoalPlan)
def remove(goal_id: int, db: Session = Depends(get_session)) -> dict:
    goal = db.get(Goal, goal_id)
    if goal is not None:
        db.delete(goal)
        db.commit()
    return goal_plan(db)


@router.put("/goals-order", response_model=GoalPlan)
def reorder(ids: list[int], db: Session = Depends(get_session)) -> dict:
    """The whole order at once, first to last."""
    for position, goal_id in enumerate(ids):
        goal = db.get(Goal, goal_id)
        if goal is not None:
            goal.sort_order = position
    db.commit()
    return goal_plan(db)
