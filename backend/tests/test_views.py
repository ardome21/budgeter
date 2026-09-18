"""The three questions the app answers, and the one rule they all share."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from backend import queries
from backend.models import Balance, Budget, Holding, Security, SecurityKind

MONTH = date(2026, 3, 1)


def test_spending_total_excludes_transfers(
    session: Session, add_transaction, account, spending_category, transfer_category
):
    """The bug this whole schema was reshaped around.

    A credit card payment is last month's spending moving between two accounts
    you own. Counting it here reported August 2026 as $2,278.84 when $207.45 of
    it was the Discover payment — and made that payment the second largest
    "purchase" of the month, ranked above the power bill and describing nothing.
    """
    add_transaction(
        account=account, category=spending_category,
        amount="52.10", occurred_on=MONTH,
    )
    add_transaction(
        account=account, category=transfer_category,
        amount="207.45", occurred_on=MONTH, description="DISCOVER PAYMENT",
    )

    summary = queries.month_summary(session, MONTH)

    assert summary["spent"] == Decimal("52.10")
    assert summary["transferred"] == Decimal("207.45")
    # Both rows still count. Summing conditionally rather than filtering is
    # what keeps a month listed at its true number of rows.
    assert summary["transaction_count"] == 2


def test_income_is_reported_positive(
    session: Session, add_transaction, account, income_category
):
    """Plaid reports money entering an account as negative, and the sign is
    kept all the way to storage. It is flipped exactly once, here, so no caller
    has to decide whether it already has been."""
    add_transaction(
        account=account, category=income_category,
        amount="-3200.00", occurred_on=MONTH, description="PAYROLL",
    )

    assert queries.month_summary(session, MONTH)["income"] == Decimal("3200.00")


def test_month_boundaries_are_half_open(
    session: Session, add_transaction, account, spending_category
):
    """The last day of a month belongs to that month and to no other."""
    for day, amount in ((date(2026, 2, 28), "1.00"), (date(2026, 3, 31), "2.00"),
                        (date(2026, 4, 1), "4.00")):
        add_transaction(
            account=account, category=spending_category,
            amount=amount, occurred_on=day,
        )

    assert queries.month_summary(session, MONTH)["spent"] == Decimal("2.00")


def test_budgeted_category_with_no_spending_is_still_listed(
    session: Session, add_transaction, spending_category
):
    """A target you have not touched is exactly the row worth seeing. An inner
    join from transactions hides it."""
    session.add(
        Budget(category_id=spending_category.id, month=MONTH, amount=Decimal(400))
    )
    session.flush()

    rows = queries.month_summary(session, MONTH)["categories"]
    row = next(r for r in rows if r["category_id"] == spending_category.id)

    assert row["spent"] == Decimal("0.00")
    assert row["remaining"] == Decimal(400)


def test_no_budget_means_remaining_is_null_not_zero(
    session: Session, add_transaction, account, spending_category
):
    """Null means "no target set", which is a different claim from "nothing
    left" and must not render as an empty progress bar."""
    add_transaction(
        account=account, category=spending_category,
        amount="10.00", occurred_on=MONTH,
    )

    rows = queries.month_summary(session, MONTH)["categories"]
    row = next(r for r in rows if r["category_id"] == spending_category.id)

    assert row["budget"] is None
    assert row["remaining"] is None


def test_net_worth_nets_liabilities(
    session: Session, add_transaction, account
):
    """Liabilities are stored negative, once, at the point of writing — so the
    series is a plain sum and no reader has to remember the sign rule."""
    card = type(account)(
        plaid_account_id="test-card", name="Test Card", institution="Test Bank",
        account_type="credit", subtype="credit card", is_liability=True,
    )
    session.add(card)
    session.flush()

    session.add_all(
        [
            Balance(account_id=account.id, as_of=date(2026, 3, 1),
                    amount=Decimal("5000.00")),
            Balance(account_id=card.id, as_of=date(2026, 3, 1),
                    amount=Decimal("-1200.00")),
        ]
    )
    session.flush()

    latest = queries.net_worth(session)["latest"]

    assert latest["assets"] == Decimal("5000.00")
    assert latest["liabilities"] == Decimal("-1200.00")
    assert latest["net"] == Decimal("3800.00")


def test_net_worth_reports_how_many_accounts_answered(
    session: Session, add_transaction, account
):
    """A day when one of two accounts reported is not a day net worth halved.

    Without this count on every point, a partial reading and a real collapse
    are the same shape on a chart.
    """
    other = type(account)(
        plaid_account_id="test-savings", name="Savings", institution="Test Bank",
        account_type="depository", subtype="savings", is_liability=False,
    )
    session.add(other)
    session.flush()

    session.add_all(
        [
            Balance(account_id=account.id, as_of=date(2026, 3, 1),
                    amount=Decimal("100.00")),
            Balance(account_id=other.id, as_of=date(2026, 3, 1),
                    amount=Decimal("900.00")),
            Balance(account_id=account.id, as_of=date(2026, 3, 2),
                    amount=Decimal("100.00")),
        ]
    )
    session.flush()

    points = queries.net_worth(session)["points"]

    assert [p["accounts_reporting"] for p in points] == [2, 1]


def test_portfolio_uses_one_snapshot_date(
    session: Session, add_transaction, account
):
    """A position sold last month must not carry its final value forward.

    Taking the newest row per security would keep it in the total for good;
    restricting to the newest date that has holdings at all is what makes a
    sale visible as an absence.
    """
    security = Security(
        plaid_security_id="sec-1", name="Vanguard Total", ticker="VTI",
        kind=SecurityKind.ETF,
    )
    sold = Security(
        plaid_security_id="sec-2", name="Sold Fund", ticker="OLD",
        kind=SecurityKind.MUTUAL_FUND,
    )
    session.add_all([security, sold])
    session.flush()

    session.add_all(
        [
            Holding(account_id=account.id, security_id=sold.id,
                    as_of=date(2026, 2, 1), quantity=Decimal(10),
                    price=Decimal("10.0000"), value=Decimal("100.00"),
                    cost_basis=Decimal("90.00")),
            Holding(account_id=account.id, security_id=security.id,
                    as_of=date(2026, 3, 1), quantity=Decimal(5),
                    price=Decimal("200.0000"), value=Decimal("1000.00"),
                    cost_basis=Decimal("800.00")),
        ]
    )
    session.flush()

    result = queries.portfolio(session)

    assert result["as_of"] == date(2026, 3, 1)
    assert result["total_value"] == Decimal("1000.00")
    assert [h["ticker"] for h in result["holdings"]] == ["VTI"]


def test_missing_cost_basis_makes_the_total_unknown_not_wrong(
    session: Session, add_transaction, account
):
    """Not every institution reports a basis. A null is not a zero — zero would
    report the entire position as gain, and a portfolio total that quietly
    excludes a position is worse than one that admits it cannot be computed."""
    security = Security(
        plaid_security_id="sec-3", name="Employer Trust", ticker=None,
        kind=SecurityKind.OTHER,
    )
    session.add(security)
    session.flush()
    session.add(
        Holding(account_id=account.id, security_id=security.id,
                as_of=date(2026, 3, 1), quantity=Decimal(100),
                price=Decimal("22.7000"), value=Decimal("2270.00"),
                cost_basis=None)
    )
    session.flush()

    result = queries.portfolio(session)

    assert result["total_value"] == Decimal("2270.00")
    assert result["total_cost_basis"] is None
    assert result["holdings"][0]["gain"] is None
