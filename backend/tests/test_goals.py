"""Goals share one pot, funded in order, cash first."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.goals import goal_plan
from backend.models import Account, Balance, Goal, PayLine, PayLineKind, PayStub

TODAY = date(2026, 4, 10)


def _balances(session: Session, account: Account, **amounts: str) -> dict:
    made = {"checking": account}
    specs = {
        "brokerage": {"account_type": "investment", "subtype": "brokerage"},
        "plan": {
            "account_type": "investment", "subtype": "401k", "is_retirement": True
        },
        "hsa": {"account_type": "investment", "subtype": "hsa"},
    }
    for key, spec in specs.items():
        made[key] = Account(name=key, institution="Fund", **spec)
        session.add(made[key])
    session.flush()
    for key, amount in amounts.items():
        session.add(Balance(account_id=made[key].id, as_of=date(2026, 4, 9),
                            amount=Decimal(amount)))
    session.flush()
    return made


def _goal(session: Session, order: int, name: str, **fields) -> None:
    session.add(Goal(name=name, sort_order=order, **fields))
    session.flush()


def _spend_and_earn(add_transaction, account, income, spending, kept: str):
    """Three finished months, each keeping `kept` of 5000."""
    for month in (1, 2, 3):
        add_transaction(account=account, category=income, amount="-5000.00",
                        occurred_on=date(2026, month, 15))
        add_transaction(account=account, category=spending,
                        amount=str(Decimal(5000) - Decimal(kept)),
                        occurred_on=date(2026, month, 3))


def test_pot_is_cash_first_and_leaves_out_retirement_and_hsa(
    session: Session, account, add_transaction, income_category, spending_category
):
    """The car is covered, but mostly by the stock market — and says so."""
    _balances(session, account, checking="16000", brokerage="24000",
              plan="128000", hsa="8000")
    _spend_and_earn(add_transaction, account, income_category, spending_category,
                    kept="500")
    _goal(session, 0, "Cushion", months_of_spending=Decimal("3"))
    _goal(session, 1, "Car", amount=Decimal("20000"))

    plan = goal_plan(session, today=TODAY)
    cushion, car = plan["goals"]

    assert plan["cash"] == Decimal("16000")
    assert plan["invested"] == Decimal("24000")
    assert cushion["target"] == Decimal("13500.00")
    assert cushion["covered_cash"] == Decimal("13500.00")
    assert car["covered_cash"] == Decimal("2500.00")
    assert car["covered_invested"] == Decimal("17500.00")
    assert car["remaining"] == Decimal("0.00")
    assert plan["unassigned_invested"] == Decimal("6500.00")


def test_a_dated_goal_says_what_it_needs_and_whether_it_gets_it(
    session: Session, account, add_transaction, income_category, spending_category
):
    _balances(session, account, checking="0")
    _spend_and_earn(add_transaction, account, income_category, spending_category,
                    kept="500")
    # 10 months away, 6000 to go: 600 a month, against 500 put away.
    _goal(session, 0, "Wedding", amount=Decimal("6000"),
          target_date=date(2027, 2, 1))

    plan = goal_plan(session, today=TODAY)
    wedding = plan["goals"][0]

    assert plan["monthly_saving"] == Decimal("500.00")
    assert wedding["monthly_needed"] == Decimal("600.00")
    assert plan["short_by"] == Decimal("100.00")
    assert wedding["on_track"] is False
    assert wedding["reached_on"] == date(2027, 4, 1)


def test_an_undated_goal_takes_what_the_earlier_ones_leave(
    session: Session, account, add_transaction, income_category, spending_category
):
    _balances(session, account, checking="0")
    _spend_and_earn(add_transaction, account, income_category, spending_category,
                    kept="1000")
    _goal(session, 0, "Wedding", amount=Decimal("6000"),
          target_date=date(2027, 2, 1))
    _goal(session, 1, "House", amount=Decimal("10000"))

    wedding, house = goal_plan(session, today=TODAY)["goals"]

    assert wedding["on_track"] is True
    # 400 a month alongside the wedding for 10 months, then 1000: 4000 by
    # February, 6000 more by August.
    assert house["reached_on"] == date(2027, 8, 1)


def test_espp_into_the_brokerage_counts_as_reachable_saving(
    session: Session, account, add_transaction, income_category, spending_category
):
    """A 401(k) is saving, but not for a house. An ESPP buying into the
    brokerage is both."""
    made = _balances(session, account, checking="0")
    session.add(PayStub(
        effective_from=date(2026, 1, 1), periods_per_year=24, gross=Decimal("6000"),
        lines=[
            PayLine(kind=PayLineKind.SAVING, name="ESPP", amount=Decimal("100"),
                    account_id=made["brokerage"].id),
            PayLine(kind=PayLineKind.SAVING, name="401(k)", amount=Decimal("500"),
                    account_id=made["plan"].id),
        ],
    ))
    _spend_and_earn(add_transaction, account, income_category, spending_category,
                    kept="300")

    plan = goal_plan(session, today=TODAY)

    assert plan["monthly_kept"] == Decimal("300.00")
    assert plan["monthly_from_pay"] == Decimal("200.00")
    assert plan["monthly_saving"] == Decimal("500.00")


def test_goals_round_trip(client: TestClient):
    made = client.post("/api/goals", json={"name": "Car", "amount": "20000"})
    assert made.status_code == 201
    client.post("/api/goals", json={"name": "Cushion", "months_of_spending": "3"})
    ids = [g["id"] for g in client.get("/api/goals").json()["goals"]]

    reordered = client.put("/api/goals-order", json=list(reversed(ids))).json()
    assert [g["name"] for g in reordered["goals"]] == ["Cushion", "Car"]

    both = client.post("/api/goals", json={
        "name": "Bad", "amount": "1", "months_of_spending": "1"})
    assert both.status_code == 422

    left = client.delete(f"/api/goals/{ids[0]}").json()
    assert [g["name"] for g in left["goals"]] == ["Cushion"]
