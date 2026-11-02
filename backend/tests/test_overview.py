"""Where you stand, and each month as earned, spent and kept."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    Account,
    Balance,
    Contribution,
    PayLine,
    PayLineKind,
    PayStub,
)
from backend.overview import overview

TODAY = date(2026, 4, 10)


def _stub(session: Session, effective_from: date, gross: str, **lines: str) -> None:
    session.add(
        PayStub(
            effective_from=effective_from,
            periods_per_year=24,
            gross=Decimal(gross),
            lines=[
                PayLine(kind=PayLineKind[k.upper()], name=k, amount=Decimal(v))
                for k, v in lines.items()
            ],
        )
    )
    session.flush()


def _month(rows: list[dict], month: date) -> dict:
    return next(r for r in rows if r["month"] == month)


def test_kept_is_take_home_less_spending(
    session: Session, add_transaction, account, income_category, spending_category,
    transfer_category,
):
    """A card payment moves money between your own accounts. It is neither
    spent nor earned, and must not shrink what was kept."""
    add_transaction(account=account, category=income_category,
                    amount="-2500.00", occurred_on=date(2026, 3, 15))
    add_transaction(account=account, category=spending_category,
                    amount="900.00", occurred_on=date(2026, 3, 3))
    add_transaction(account=account, category=transfer_category,
                    amount="400.00", occurred_on=date(2026, 3, 20))

    march = _month(overview(session, today=TODAY)["flow"], date(2026, 3, 1))

    assert march["take_home"] == Decimal("2500.00")
    assert march["spent"] == Decimal("900.00")
    assert march["kept"] == Decimal("1600.00")
    assert march["has_stub"] is False
    assert march["savings_rate"] == Decimal("0.640")


def test_stub_counts_what_the_paycheck_put_away(
    session: Session, add_transaction, account, income_category, spending_category
):
    """The bank sees take-home only. A 401(k) and an employer match never pass
    through it, and without the stub the savings rate misses both."""
    _stub(session, date(2026, 1, 1), "4000.00",
          tax="800.00", saving="400.00", deduction="100.00", employer="200.00")
    for day in (15, 31):
        add_transaction(account=account, category=income_category,
                        amount="-2700.00", occurred_on=date(2026, 3, day))
    add_transaction(account=account, category=spending_category,
                    amount="4400.00", occurred_on=date(2026, 3, 3))

    march = _month(overview(session, today=TODAY)["flow"], date(2026, 3, 1))

    # Twice a month: every per-paycheck line doubles.
    assert march["saved_from_pay"] == Decimal("800.00")
    assert march["employer"] == Decimal("400.00")
    assert march["tax"] == Decimal("1600.00")
    # Worked back from what arrived: 5400 + 1600 + 800 + 200.
    assert march["earned"] == Decimal("8000.00")
    # Kept 1000, plus 800 from pay and 400 from the employer.
    assert march["saved"] == Decimal("2200.00")
    assert march["savings_rate"] == Decimal("0.262")


def test_stub_carries_forward_and_not_back(
    session: Session, add_transaction, account, income_category
):
    """A stub holds from its month on. A month before the first one gets no
    pre-paycheck figures at all, rather than borrowed ones."""
    _stub(session, date(2026, 3, 1), "4000.00", saving="400.00")
    for month in (2, 3):
        add_transaction(account=account, category=income_category,
                        amount="-3600.00", occurred_on=date(2026, month, 15))

    flow = overview(session, today=TODAY)["flow"]

    assert _month(flow, date(2026, 3, 1))["saved_from_pay"] == Decimal("800.00")
    assert _month(flow, date(2026, 2, 1))["has_stub"] is False
    assert _month(flow, date(2026, 2, 1))["saved_from_pay"] is None


def test_a_month_before_every_bank_started_is_marked(
    session: Session, add_transaction, account, spending_category
):
    """Averaging a month that is missing a bank's spending makes the typical
    month look cheap. The first month here starts on the 20th."""
    add_transaction(account=account, category=spending_category,
                    amount="10.00", occurred_on=date(2026, 1, 20))
    add_transaction(account=account, category=spending_category,
                    amount="3000.00", occurred_on=date(2026, 2, 5))

    result = overview(session, today=TODAY)

    assert _month(result["flow"], date(2026, 1, 1))["missing_history"] is True
    assert _month(result["flow"], date(2026, 4, 1))["partial"] is True
    # Only the two whole months are typical: Feb and March.
    assert result["typical_months"] == [date(2026, 3, 1), date(2026, 2, 1)]
    assert result["typical"]["spent"] == Decimal("1500.00")


def test_standing_buckets_and_cushion(
    session: Session, add_transaction, account, spending_category
):
    """Card debt comes off the cushion: it is spending already done."""
    card = Account(name="Card", institution="Bank", account_type="credit",
                   subtype="credit card", is_liability=True)
    plan = Account(name="401k", institution="Fund", account_type="investment",
                   subtype="401k", is_retirement=True)
    brokerage = Account(name="Brokerage", institution="Fund",
                        account_type="investment", subtype="brokerage")
    session.add_all([card, plan, brokerage])
    session.flush()
    for acct, amount in ((account, "6000"), (card, "-1000"), (plan, "50000"),
                         (brokerage, "9000")):
        session.add(Balance(account_id=acct.id, as_of=date(2026, 4, 9),
                            amount=Decimal(amount)))
    # An older reading is not the standing.
    session.add(Balance(account_id=account.id, as_of=date(2026, 4, 1),
                        amount=Decimal("99999")))
    add_transaction(account=account, category=spending_category,
                    amount="2000.00", occurred_on=date(2026, 3, 1))
    session.flush()

    result = overview(session, today=TODAY)

    assert result["cash"] == Decimal("6000")
    assert result["owed"] == Decimal("-1000")
    assert result["retirement"] == Decimal("50000")
    assert result["invested"] == Decimal("9000")
    assert result["net_worth"] == Decimal("64000")
    assert result["cushion"] == Decimal("5000")
    assert result["months_covered"] == Decimal("2.5")


def test_pay_stub_round_trip(client: TestClient):
    body = {
        "periods_per_year": 24,
        "gross": "4000.00",
        "lines": [
            {"kind": "TAX", "name": "Federal", "amount": "600.00"},
            {"kind": "SAVING", "name": "401(k)", "amount": "400.00"},
            {"kind": "EMPLOYER", "name": "Match", "amount": "200.00"},
        ],
    }
    saved = client.put("/api/pay/2026/3", json=body)
    assert saved.status_code == 200
    # The employer's money is on top of gross, not out of it.
    assert saved.json()["take_home"] == "3000.00"

    # Saving again replaces, never duplicates.
    client.put("/api/pay/2026/3", json={**body, "gross": "4100.00"})
    stubs = client.get("/api/pay").json()
    assert [s["gross"] for s in stubs] == ["4100.00"]

    assert client.delete("/api/pay/2026/3").status_code == 204
    assert client.get("/api/pay").json() == []


def test_pay_stub_refuses_more_out_than_in(client: TestClient):
    body = {"periods_per_year": 24, "gross": "100.00",
            "lines": [{"kind": "TAX", "name": "Tax", "amount": "150.00"}]}
    assert client.put("/api/pay/2026/3", json=body).status_code == 422


def test_typical_does_not_mix_months_with_and_without_a_stub(
    session: Session, add_transaction, account, income_category, spending_category
):
    """With a stub covering two of the three typical months, the paycheck's
    saving cannot be averaged in for some months and set over take-home."""
    _stub(session, date(2026, 2, 1), "4000.00", saving="500.00")
    for month in (1, 2, 3):
        add_transaction(account=account, category=income_category,
                        amount="-7000.00", occurred_on=date(2026, month, 15))
        add_transaction(account=account, category=spending_category,
                        amount="6000.00", occurred_on=date(2026, month, 3))

    typical = overview(session, today=TODAY)["typical"]

    assert typical["earned"] is None
    assert typical["saved"] == Decimal("1000.00")
    assert typical["savings_rate"] == Decimal("0.143")


def test_month_so_far_counts_only_paychecks_that_landed(
    session: Session, add_transaction, account, income_category
):
    _stub(session, date(2026, 1, 1), "4000.00", tax="500.00", saving="500.00")
    april = _month(overview(session, today=TODAY)["flow"], date(2026, 4, 1))
    assert april["saved_from_pay"] == Decimal("0.00")
    assert april["earned"] == Decimal("0.00")

    add_transaction(account=account, category=income_category,
                    amount="-3000.00", occurred_on=date(2026, 4, 1))
    april = _month(overview(session, today=TODAY)["flow"], date(2026, 4, 1))
    # One of two paychecks.
    assert april["saved_from_pay"] == Decimal("500.00")


def test_pay_breakdown_adds_up_to_earned(
    session: Session, add_transaction, account, income_category
):
    """Interest beyond the stub's take-home is its own line, so the lines
    reconcile to Earned rather than leaving a gap to go looking for."""
    _stub(session, date(2026, 1, 1), "4000.00",
          tax="800.00", saving="400.00", deduction="100.00", employer="200.00")
    for month in (1, 2, 3):
        for day in (14, 28):
            add_transaction(account=account, category=income_category,
                            amount="-2700.00", occurred_on=date(2026, month, day))
        add_transaction(account=account, category=income_category,
                        amount="-25.00", occurred_on=date(2026, month, 28),
                        description="Monthly Interest Paid")

    result = overview(session, today=TODAY)
    b = result["pay_breakdown"]

    assert b["gross"] == Decimal("8000.00")
    kinds = [g["kind"] for g in b["groups"]]
    assert kinds == ["TAX", "SAVING", "DEDUCTION", "EMPLOYER"]
    assert b["take_home_pay"] == Decimal("5400.00")
    assert b["other_income"] == Decimal("25.00")
    assert b["earned"] == result["typical"]["earned"] == Decimal("8025.00")


def test_no_breakdown_until_every_typical_month_has_a_stub(
    session: Session, add_transaction, account, income_category
):
    _stub(session, date(2026, 3, 1), "4000.00", tax="800.00")
    for month in (1, 2, 3):
        add_transaction(account=account, category=income_category,
                        amount="-3200.00", occurred_on=date(2026, month, 15))
    assert overview(session, today=TODAY)["pay_breakdown"] is None


def test_employer_money_is_measured_from_the_plan(
    session: Session, add_transaction, account, income_category
):
    """Everything paid into the plan, less your own lines into it. That is the
    match on each paycheck and the contribution on a day that was not a
    payday, with nothing typed for either — the 2026 Moody's figures."""
    plan = Account(name="Moody's plan", institution="Fidelity",
                   account_type="investment", subtype="profit sharing plan",
                   is_retirement=True)
    session.add(plan)
    session.flush()
    session.add(PayStub(
        effective_from=date(2026, 1, 1), periods_per_year=24,
        gross=Decimal("4427.96"),
        lines=[
            PayLine(kind=PayLineKind.SAVING, name="401(k) Pretax",
                    amount=Decimal("442.80"), account_id=plan.id),
            PayLine(kind=PayLineKind.SAVING, name="401(k) Roth",
                    amount=Decimal("265.68"), account_id=plan.id),
            # Paid somewhere else: not part of the plan's sum.
            PayLine(kind=PayLineKind.SAVING, name="HSA", amount=Decimal("83.34")),
        ],
    ))
    for i, (day, amount) in enumerate(
        ((9, "944.05"), (15, "974.16"), (31, "974.16"))
    ):
        session.add(Contribution(
            account_id=plan.id, plaid_investment_transaction_id=f"c{i}",
            occurred_on=date(2026, 3, day), amount=Decimal(amount),
            description="contribution",
        ))
    for day in (15, 31):
        add_transaction(account=account, category=income_category,
                        amount="-2464.79", occurred_on=date(2026, 3, day))
    session.flush()

    march = _month(overview(session, today=TODAY)["flow"], date(2026, 3, 1))

    # 2892.37 in, 1416.96 of it yours: 531.36 of match plus the 944.05.
    assert march["employer"] == Decimal("1475.41")
    assert march["saved_from_pay"] == Decimal("1583.64")


def test_a_late_contribution_never_makes_the_employer_take_money_back(
    session: Session, add_transaction, account, income_category
):
    """Nothing reached the plan this month — it posts on the 1st of the next.
    The employer's share is zero, not minus your 401(k)."""
    add_transaction(account=account, category=income_category,
                    amount="-3600.00", occurred_on=date(2026, 3, 15))
    plan = Account(name="Plan", institution="Fund", account_type="investment",
                   subtype="401k", is_retirement=True)
    session.add(plan)
    session.flush()
    _stub(session, date(2026, 1, 1), "4000.00")
    stub = session.scalar(select(PayStub))
    stub.lines.append(
        PayLine(kind=PayLineKind.SAVING, name="401(k)", amount=Decimal("400.00"),
                account_id=plan.id)
    )
    session.flush()

    march = _month(overview(session, today=TODAY)["flow"], date(2026, 3, 1))
    assert march["employer"] == Decimal("0.00")


def test_a_contribution_posted_late_counts_with_its_paycheck(
    session: Session, add_transaction, account, income_category
):
    """Fidelity posted the July 31 paycheck's contribution on August 3."""
    plan = Account(name="Plan", institution="Fund", account_type="investment",
                   subtype="401k", is_retirement=True)
    session.add(plan)
    session.flush()
    session.add(PayStub(
        effective_from=date(2026, 1, 1), periods_per_year=24, gross=Decimal("4000"),
        lines=[PayLine(kind=PayLineKind.SAVING, name="401(k)",
                       amount=Decimal("400.00"), account_id=plan.id)],
    ))
    for month, day in ((2, 15), (2, 28), (3, 15), (3, 31)):
        add_transaction(account=account, category=income_category,
                        amount="-3000.00", occurred_on=date(2026, month, day))
    for i, on in enumerate((date(2026, 2, 15), date(2026, 3, 2), date(2026, 3, 15),
                            date(2026, 4, 2))):
        session.add(Contribution(
            account_id=plan.id, plaid_investment_transaction_id=f"late{i}",
            occurred_on=on, amount=Decimal("500.00"), description="contribution",
        ))
    session.flush()

    flow = overview(session, today=TODAY)["flow"]
    # Two paychecks a month, each with 100 of match on top of 400 of yours.
    assert _month(flow, date(2026, 2, 1))["employer"] == Decimal("200.00")
    assert _month(flow, date(2026, 3, 1))["employer"] == Decimal("200.00")
