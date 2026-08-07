"""Tests for the workbook importer's pure helpers.

The importer itself needs the real spreadsheets, which aren't in the repo, so
what's covered here is the decision logic that turns messy cells into records —
the part that actually decides whether a number lands in the right month.

Every case below is a real string or value taken from the source workbooks.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from import_workbook import money, parse_text_date, sheet_month

from backend.merchants import normalize_merchant


class TestMoney:
    def test_excel_float_error_is_quantized_away(self):
        # The workbook literally contains this value for monthly fixed costs.
        assert money(1943.9000000000003) == Decimal("1943.90")

    def test_returns_decimal_not_float(self):
        assert isinstance(money(12.5), Decimal)

    def test_negative_amounts_survive(self):
        # Refunds are negative; they must not be coerced or dropped.
        assert money(-889.67) == Decimal("-889.67")

    @pytest.mark.parametrize("junk", [None, "#REF!", "NA", "`"])
    def test_non_numeric_cells_are_rejected(self, junk):
        assert money(junk) is None

    def test_zero_is_a_value_not_an_absence(self):
        assert money(0) == Decimal("0.00")


class TestSheetMonth:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("July Spending", 7),
            ("January Spending ", 1),  # trailing space, as in the 2024 workbook
            ("September Spending ", 9),
            ("August Budget", 8),
            ("OLD Spending", None),  # no month in the name
            ("Accounts", None),
        ],
    )
    def test_month_from_sheet_name(self, title, expected):
        assert sheet_month(title) == expected


class TestParseTextDate:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Amount As of Feb 2", date(2024, 2, 2)),
            ("March 2nd", date(2024, 3, 2)),
            ("April 3rd", date(2024, 4, 3)),
            ("October 17th", date(2024, 10, 17)),
        ],
    )
    def test_prose_headers_from_the_2024_accounts_sheet(self, header, expected):
        assert parse_text_date(header, 2024) == expected

    def test_real_datetime_passes_through(self):
        assert parse_text_date(datetime(2024, 6, 3), 2024) == date(2024, 6, 3)

    @pytest.mark.parametrize("junk", [None, "Institution", "is_retirement", 42])
    def test_non_dates_return_none(self, junk):
        assert parse_text_date(junk, 2024) is None


class TestNormalizeMerchant:
    def test_store_numbers_city_and_state_collapse(self):
        forms = [
            "HARRIS TEETER #412",
            "HARRIS TEETER #412 CHARLOTTE NC",
            "HARRIS TEETER #0061 CHARLOTTE NC",
            "Harris Teeter",
        ]
        assert len({normalize_merchant(f) for f in forms}) == 1

    def test_processor_prefix_is_stripped(self):
        assert normalize_merchant("TST* CONDADO TACOS - SOU") == normalize_merchant(
            "Condado Tacos - Sou"
        )

    def test_phone_numbers_are_stripped(self):
        assert normalize_merchant("UBER *EATS 866-576-1039 CA") == "uber eats"

    def test_per_transaction_ids_are_stripped(self):
        # Apple appends a unique id to every charge; without stripping it,
        # each subscription payment would become its own merchant.
        a = normalize_merchant("APPLE.COM/BILL 866-712-7753 CAMMGGH21Q0DA0")
        b = normalize_merchant("APPLE.COM/BILL 866-712-7753 CAMMGGQWSBX6A0")
        assert a == b

    def test_distinct_merchants_are_not_merged(self):
        # The normalizer is deliberately conservative: leaving two spellings
        # unmerged is recoverable, merging two real merchants is not.
        assert normalize_merchant("Harris Teeter") != normalize_merchant("Target")
        assert normalize_merchant("UBER *EATS 866-576-1039 CA") != normalize_merchant(
            "UBER *TRIP 866-576-1039 CA"
        )

    def test_empty_input_yields_empty_key(self):
        assert normalize_merchant("   ") == ""
