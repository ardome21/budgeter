"""The three questions the app answers, and the one rule they all share."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
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


def test_net_worth_narrows_to_the_accounts_asked_for(
    client: TestClient, session: Session, account
):
    """Card debt on its own, over time. The sum is the server's: the browser
    has no decimal type to add money with."""
    cards = [
        type(account)(
            plaid_account_id=f"test-card-{n}", name=f"Card {n}",
            institution="Test Bank", account_type="credit",
            subtype="credit card", is_liability=True,
        )
        for n in (1, 2)
    ]
    session.add_all(cards)
    session.flush()
    session.add_all(
        [
            Balance(account_id=account.id, as_of=date(2026, 3, 1),
                    amount=Decimal("5000.00")),
            Balance(account_id=cards[0].id, as_of=date(2026, 3, 1),
                    amount=Decimal("-537.24")),
            Balance(account_id=cards[1].id, as_of=date(2026, 3, 1),
                    amount=Decimal("-411.34")),
        ]
    )
    session.flush()

    body = client.get(
        "/api/net-worth", params=[("account", c.id) for c in cards]
    ).json()

    assert body["latest"]["net"] == "-948.58"
    assert body["latest"]["assets"] == "0.00"
    # The breakdown the hover shows adds up, server-side, to the same figure.
    [cards_group] = body["latest"]["groups"]
    assert cards_group["group"] == "credit"
    assert cards_group["total"] == "-948.58"
    assert [a["amount"] for a in cards_group["accounts"]] == ["-537.24", "-411.34"]
    assert body["latest"]["accounts_reporting"] == 2
    assert body["account_count"] == 2


def test_accounts_are_grouped_by_what_the_bank_reports(
    client: TestClient, session: Session, account
):
    """Retirement wins over investment: a Roth IRA is invested, but it is
    retirement money first. An HSA is not flagged retirement, so it is an
    investment."""
    def add(name, kind, subtype, *, liability=False, retirement=False):
        session.add(
            type(account)(
                plaid_account_id=f"test-{name}", name=name,
                institution="Test Bank", account_type=kind, subtype=subtype,
                is_liability=liability, is_retirement=retirement,
            )
        )

    add("Savings", "depository", "savings")
    add("Card", "credit", "credit card", liability=True)
    add("Roth", "investment", "roth", retirement=True)
    add("HSA", "investment", "hsa")
    session.flush()

    groups = {a["name"]: a["group"] for a in client.get("/api/accounts").json()}

    assert groups == {
        "Test Checking": "checking",
        "Savings": "savings",
        "Card": "credit",
        "Roth": "retirement",
        "HSA": "investment",
    }


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


def test_a_closed_account_does_not_make_later_days_look_partial(
    session: Session, account
):
    """Hiding an account stops it reporting. Counted, the days before would
    out-number every day after, and every new day would read as incomplete.
    Its balance still counts toward the day it was read."""
    hidden = type(account)(
        plaid_account_id="test-billpay", name="BillPay", institution="Test Bank",
        account_type="depository", subtype="checking", is_liability=False,
        closed_on=date(2026, 3, 2),
    )
    session.add(hidden)
    session.flush()

    session.add_all(
        [
            Balance(account_id=account.id, as_of=date(2026, 3, 1),
                    amount=Decimal("100.00")),
            Balance(account_id=hidden.id, as_of=date(2026, 3, 1),
                    amount=Decimal("5.00")),
            Balance(account_id=account.id, as_of=date(2026, 3, 2),
                    amount=Decimal("100.00")),
        ]
    )
    session.flush()

    points = queries.net_worth(session)["points"]

    assert [p["accounts_reporting"] for p in points] == [1, 1]
    assert points[0]["net"] == Decimal("105.00")


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


def test_a_money_market_basis_is_its_own_value(session: Session, account):
    """Fidelity reports null for FDRXX and a cash balance because there is
    nothing to report — a money market is $1.00 a unit by construction, so what
    was paid in *is* what it is worth. Reading that null as "unknown" is what
    nulled the cost basis of half the accounts, and the headline growth figure
    with them, the last time this was computed.

    Derived on read. The stored row keeps the institution's null, so nothing in
    the table is ever an inference of ours.
    """
    cash = Security(
        plaid_security_id="sec-cash", name="FDRXX", ticker="FDRXX",
        kind=SecurityKind.CASH, is_cash_equivalent=True,
    )
    session.add(cash)
    session.flush()
    session.add(
        Holding(account_id=account.id, security_id=cash.id, as_of=date(2026, 3, 1),
                quantity=Decimal("8063.08"), price=Decimal("1.0000"),
                value=Decimal("8063.08"), cost_basis=None)
    )
    session.flush()

    row = queries.portfolio(session)["holdings"][0]

    assert row["cost_basis"] == Decimal("8063.08")
    assert row["gain"] == Decimal("0.00")
    # Untouched in the table: the null is the institution's answer, not ours.
    assert session.scalar(select(Holding.cost_basis)) is None


def test_the_basis_total_says_how_much_it_covers(session: Session, account):
    """The real portfolio has no basis for the employer-plan trust that holds
    three quarters of it. A "paid in" figure that silently describes the other
    quarter is worse than one that says so."""
    plan = Security(
        plaid_security_id="sec-plan", name="FID FRDM INX 2065",
        ticker=None, kind=SecurityKind.MUTUAL_FUND,
    )
    fund = Security(
        plaid_security_id="sec-fund", name="Fidelity Large Cap", ticker="FELC",
        kind=SecurityKind.ETF,
    )
    session.add_all([plan, fund])
    session.flush()
    session.add_all(
        [
            Holding(account_id=account.id, security_id=plan.id,
                    as_of=date(2026, 3, 1), quantity=Decimal(1),
                    price=None, value=Decimal("75.00"), cost_basis=None),
            Holding(account_id=account.id, security_id=fund.id,
                    as_of=date(2026, 3, 1), quantity=Decimal(1),
                    price=None, value=Decimal("25.00"),
                    cost_basis=Decimal("20.00")),
        ]
    )
    session.flush()

    result = queries.portfolio(session)

    assert result["total_value"] == Decimal("100.00")
    assert result["total_cost_basis"] == Decimal("20.00")
    # …and it accounts for 25 of the 100, which is the half that makes it
    # readable rather than misleading.
    assert result["cost_basis_covers"] == Decimal("25.00")


def test_expected_to_date_is_the_budget_spread_over_the_month(
    session: Session, account, spending_category, add_transaction
):
    """Mid-month, "are you within budget" is a question about pace. Spent
    against budget only answers it once you are already over."""
    session.add(
        Budget(
            category_id=spending_category.id,
            month=date(2026, 6, 1),
            amount=Decimal("300.00"),
        )
    )
    add_transaction(
        account=account, category=spending_category,
        amount="120.00", occurred_on=date(2026, 6, 5),
    )
    session.flush()

    # June has 30 days; on the 10th, a $300 budget allows $100.
    summary = queries.month_summary(session, date(2026, 6, 1), today=date(2026, 6, 10))

    assert summary["days_in_month"] == 30
    assert summary["days_elapsed"] == 10
    assert summary["expected_to_date"] == Decimal("100.00")
    # $120 spent against $100 allowed: ahead of pace, nowhere near over budget.
    assert summary["over_pace"] is True
    assert summary["over_budget"] is False


def test_a_finished_month_is_wholly_elapsed(
    session: Session, account, spending_category
):
    """Otherwise every past month reads as ahead of pace forever, because
    'today' is past its end and the fraction would keep growing."""
    session.add(
        Budget(
            category_id=spending_category.id,
            month=date(2026, 6, 1),
            amount=Decimal("300.00"),
        )
    )
    session.flush()

    summary = queries.month_summary(session, date(2026, 6, 1), today=date(2026, 9, 18))

    assert summary["days_elapsed"] == 30
    assert summary["expected_to_date"] == Decimal("300.00")


def test_nothing_budgeted_makes_no_claim_about_pace(
    session: Session, account, spending_category, add_transaction
):
    """"On track" against no target is not a statement worth rendering."""
    add_transaction(
        account=account, category=spending_category,
        amount="120.00", occurred_on=date(2026, 6, 5),
    )

    summary = queries.month_summary(session, date(2026, 6, 1), today=date(2026, 6, 10))

    assert summary["expected_to_date"] is None
    assert summary["over_pace"] is False


def test_budgeted_counts_spending_targets_only(
    session: Session, spending_category, income_category
):
    """It is printed as "of X budgeted" beside a spending-only total, so a
    savings or income target in that figure made one sentence measure two
    different things."""
    session.add_all([
        Budget(category_id=spending_category.id, month=date(2026, 6, 1),
               amount=Decimal("300.00")),
        Budget(category_id=income_category.id, month=date(2026, 6, 1),
               amount=Decimal("4000.00")),
    ])
    session.flush()

    summary = queries.month_summary(session, date(2026, 6, 1))

    assert summary["budgeted"] == Decimal("300.00")


def test_a_budget_suggestion_is_the_median_not_the_mean(
    session: Session, account, spending_category, add_transaction
):
    """Three months of 100, 100 and 700 is a habit of 100 and one holiday. The
    mean proposes 300 — a budget you would never meet and never trust."""
    for month, amount in ((6, "100.00"), (7, "100.00"), (8, "700.00")):
        add_transaction(
            account=account, category=spending_category,
            amount=amount, occurred_on=date(2026, month, 5),
        )

    rows = queries.suggest_budgets(session, date(2026, 9, 1))

    assert [r["amount"] for r in rows] == [Decimal("100.00")]
    assert rows[0]["months"] == 3


def test_a_category_seen_once_is_not_a_monthly_habit(
    session: Session, account, spending_category, add_transaction
):
    """One December flight must not propose itself as a monthly budget: the
    two months it did not appear in are zeroes, and the median knows it."""
    add_transaction(
        account=account, category=spending_category,
        amount="900.00", occurred_on=date(2026, 8, 5),
    )

    rows = queries.suggest_budgets(session, date(2026, 9, 1))

    assert rows[0]["amount"] == Decimal("0.00")
    assert rows[0]["months"] == 1


def test_the_month_being_budgeted_is_not_its_own_evidence(
    session: Session, account, spending_category, add_transaction
):
    """It is partway through, so including it would propose a budget lower
    than anything you have ever actually spent."""
    add_transaction(
        account=account, category=spending_category,
        amount="500.00", occurred_on=date(2026, 9, 2),
    )

    rows = queries.suggest_budgets(session, date(2026, 9, 1))

    assert rows == []


# --- what the month adds up to ----------------------------------------------


def _row(summary, category):
    return next(r for r in summary["categories"] if r["category_id"] == category.id)


def _monthly(add, account, category, merchant, amount, day, months):
    for m in months:
        add(
            account=account, category=category, amount=amount,
            occurred_on=date(2025, m, day), merchant=merchant,
        )


def test_a_charge_from_each_of_the_last_three_months_is_still_to_come(
    session: Session, add_transaction, account, spending_category
):
    """Rent and subscriptions have not landed on the 2nd, and a Left that
    ignores them makes the start of every month look rich."""
    add, cat = add_transaction, spending_category
    _monthly(add, account, cat, "Rent", "1600.00", 3, [6, 7, 8])
    _monthly(add, account, cat, "Spotify", "12.99", 12, [6, 7, 8])

    summary = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 9, 2))

    assert [u["merchant"] for u in summary["upcoming"]] == ["Rent", "Spotify"]
    assert summary["upcoming_total"] == Decimal("1612.99")
    assert summary["left_after_upcoming"] == summary["left"] - Decimal("1612.99")


def test_a_bill_that_has_already_come_this_month_is_not_still_to_come(
    session: Session, add_transaction, account, spending_category
):
    add, cat = add_transaction, spending_category
    _monthly(add, account, cat, "Spotify", "12.99", 12, [6, 7, 8, 9])

    summary = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 9, 20))

    assert summary["upcoming"] == []


def test_a_bill_past_its_day_is_marked_late_rather_than_dropped(
    session: Session, add_transaction, account, spending_category
):
    add, cat = add_transaction, spending_category
    _monthly(add, account, cat, "Netflix", "21.64", 1, [6, 7, 8])

    summary = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 9, 5))

    assert summary["upcoming"][0]["late"] is True


def test_somewhere_shopped_at_often_is_not_a_bill(
    session: Session, add_transaction, account, spending_category
):
    """A supermarket visited weekly always has some charge that lines up with
    last month's by chance. On the real data that made it three bills."""
    for m in (6, 7, 8):
        for day in (2, 9, 16, 23):
            add_transaction(
                account=account, category=spending_category, amount="40.00",
                occurred_on=date(2025, m, day), merchant="Harris Teeter",
            )

    summary = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 9, 1))

    assert summary["upcoming"] == []


def test_an_amount_that_wanders_is_not_predicted(
    session: Session, add_transaction, account, spending_category
):
    for m, amount in ((6, "147.25"), (7, "85.49"), (8, "285.99")):
        add_transaction(
            account=account, category=spending_category, amount=amount,
            occurred_on=date(2025, m, 10), merchant="Duke Energy",
        )

    summary = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 9, 1))

    assert summary["upcoming"] == []


def test_a_running_month_is_compared_with_last_month_to_the_same_day(
    session: Session, add_transaction, account, spending_category
):
    """The whole of October against two days of November says November is
    cheap, every month, until about the 25th."""
    add_transaction(account=account, category=spending_category, amount="30.00",
                    occurred_on=date(2025, 8, 1))
    add_transaction(account=account, category=spending_category, amount="500.00",
                    occurred_on=date(2025, 8, 20))
    add_transaction(account=account, category=spending_category, amount="45.00",
                    occurred_on=date(2025, 9, 2))

    running = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 9, 2))
    row = _row(running, spending_category)

    assert running["compared_through"] == 2
    assert running["previous_spent"] == Decimal("30.00")
    assert row["previous"] == Decimal("30.00")
    assert row["change"] == Decimal("15.00")

    over = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 10, 5))
    assert over["compared_through"] is None
    assert over["previous_spent"] == Decimal("530.00")


def test_the_daily_line_is_running_spending_with_quiet_days_flat(
    session: Session, add_transaction, account, spending_category, transfer_category
):
    add_transaction(account=account, category=spending_category, amount="10.00",
                    occurred_on=date(2025, 9, 1))
    add_transaction(account=account, category=transfer_category, amount="900.00",
                    occurred_on=date(2025, 9, 2))
    add_transaction(account=account, category=spending_category, amount="5.00",
                    occurred_on=date(2025, 9, 3))

    summary = queries.month_summary(session, date(2025, 9, 1), today=date(2025, 9, 3))

    assert summary["daily"] == [Decimal("10.00"), Decimal("10.00"), Decimal("15.00")]
    assert len(summary["previous_daily"]) == 31


def test_each_category_names_where_its_money_went(
    session: Session, add_transaction, account, spending_category
):
    charges = (("Cava", "12.00"), ("Cava", "14.00"), ("Chipotle", "30.00"))
    for merchant, amount in charges:
        add_transaction(account=account, category=spending_category, amount=amount,
                        occurred_on=date(2025, 9, 4), merchant=merchant)

    summary = queries.month_summary(
        session, date(2025, 9, 1), today=date(2025, 9, 30)
    )
    row = _row(summary, spending_category)

    assert row["merchants"] == [
        {"name": "Chipotle", "spent": Decimal("30.00"), "count": 1},
        {"name": "Cava", "spent": Decimal("26.00"), "count": 2},
    ]
    assert summary["biggest"][0]["merchant"] == "Chipotle"
