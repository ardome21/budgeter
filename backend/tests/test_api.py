"""API behaviour tests.

These run against the real database inside a rolled-back transaction, so they
assert on real Postgres constraints rather than a stand-in.
"""

from decimal import Decimal

import pytest


class TestReadViews:
    def test_categories_are_ordered(self, client):
        rows = client.get("/api/categories").json()
        assert rows, "expected seeded categories"
        assert rows == sorted(rows, key=lambda c: (c["sort_order"], c["name"]))

    def test_money_crosses_the_wire_as_a_string(self, client):
        """A JSON number would become a float in the browser and lose cents."""
        body = client.get("/api/overview").json()
        assert isinstance(body["take_home"], str)
        assert Decimal(body["take_home"]) > 0

    def test_month_summary_reconciles_with_its_own_categories(self, client):
        body = client.get("/api/periods/2026/7/summary").json()
        total = sum(Decimal(c["spent"]) for c in body["categories"])
        assert total == Decimal(body["spent_total"])

    def test_committed_and_flexible_partition_the_month(self, client):
        body = client.get("/api/periods/2026/7/summary").json()
        split = body["commitment"]
        assert Decimal(split["committed"]) + Decimal(split["flexible"]) == Decimal(
            body["spent_total"]
        )

    def test_unknown_month_is_404_not_an_empty_summary(self, client):
        assert client.get("/api/periods/1999/1/summary").status_code == 404

    def test_month_out_of_range_is_rejected(self, client):
        assert client.get("/api/periods/2026/13/summary").status_code == 422


class TestCreateTransaction:
    def test_creates_and_derives_the_period_from_the_date(self, client, category_id):
        r = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-14",
                "raw_description": "TEST HARRIS TEETER #999 CHARLOTTE NC",
                "category_id": category_id,
                "amount": "12.34",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert (body["year"], body["month"]) == (2026, 3)
        assert body["amount"] == "12.34"
        assert body["source"] == "MANUAL"

    def test_undated_row_needs_an_explicit_month(self, client, category_id):
        payload = {
            "raw_description": "no date given",
            "category_id": category_id,
            "amount": "5.00",
        }
        assert client.post("/api/transactions", json=payload).status_code == 422

        payload |= {"year": 2026, "month": 4}
        r = client.post("/api/transactions", json=payload)
        assert r.status_code == 201
        assert r.json()["occurred_on"] is None
        assert (r.json()["year"], r.json()["month"]) == (2026, 4)

    def test_zero_amount_is_rejected(self, client, category_id):
        r = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-14",
                "raw_description": "nothing",
                "category_id": category_id,
                "amount": "0",
            },
        )
        assert r.status_code == 422

    def test_unknown_category_is_rejected(self, client):
        r = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-14",
                "raw_description": "x",
                "category_id": 999999,
                "amount": "1.00",
            },
        )
        assert r.status_code == 422

    def test_identical_manual_entries_both_survive(self, client, category_id):
        """Two rounds at the same bar are two transactions, not one."""
        payload = {
            "occurred_on": "2026-03-14",
            "raw_description": "PROHIBITION BAR TEST",
            "category_id": category_id,
            "amount": "8.95",
        }
        first = client.post("/api/transactions", json=payload).json()
        second = client.post("/api/transactions", json=payload).json()
        assert first["id"] != second["id"]

    def test_refunds_are_negative_amounts(self, client, category_id):
        r = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-14",
                "raw_description": "TEST REFUND",
                "category_id": category_id,
                "amount": "-25.50",
            },
        )
        assert r.status_code == 201
        assert r.json()["amount"] == "-25.50"


class TestEditTransaction:
    def _make(self, client, category_id, **overrides):
        payload = {
            "occurred_on": "2026-03-14",
            "raw_description": "EDITABLE ROW",
            "category_id": category_id,
            "amount": "10.00",
        } | overrides
        return client.post("/api/transactions", json=payload).json()

    def test_changing_the_date_moves_the_period(self, client, category_id):
        txn = self._make(client, category_id)
        r = client.patch(
            f"/api/transactions/{txn['id']}", json={"occurred_on": "2026-05-02"}
        )
        assert r.status_code == 200
        assert (r.json()["year"], r.json()["month"]) == (2026, 5)

    def test_patch_leaves_unspecified_fields_alone(self, client, category_id):
        txn = self._make(client, category_id)
        r = client.patch(f"/api/transactions/{txn['id']}", json={"amount": "99.99"})
        assert r.json()["amount"] == "99.99"
        assert r.json()["raw_description"] == txn["raw_description"]

    def test_delete_then_missing(self, client, category_id):
        txn = self._make(client, category_id)
        assert client.delete(f"/api/transactions/{txn['id']}").status_code == 204
        assert (
            client.patch(
                f"/api/transactions/{txn['id']}", json={"amount": "1.00"}
            ).status_code
            == 404
        )


class TestCsvImport:
    CSV = (
        "Transaction Date,Description,Amount\n"
        "07/03/2026,HARRIS TEETER #412 CHARLOTTE NC,66.57\n"
        "07/04/2026,TST* CONDADO TACOS - SOU,43.91\n"
        '07/05/2026,"AMAZON PRIME*CP54Z0R83","$16.23"\n'
    )

    def test_preview_writes_nothing_and_detects_columns(self, client):
        before = len(client.get("/api/transactions?limit=1000").json())
        body = client.post("/api/imports/preview", data={"text": self.CSV}).json()
        assert body["errors"] == []
        assert body["detected_columns"]["description"] == "Description"
        assert len(body["rows"]) == 3
        after = len(client.get("/api/transactions?limit=1000").json())
        assert before == after

    def test_preview_infers_categories_from_imported_history(self, client):
        body = client.post("/api/imports/preview", data={"text": self.CSV}).json()
        harris = next(r for r in body["rows"] if "HARRIS" in r["raw_description"])
        # The workbook import learned this merchant across 110 transactions.
        assert harris["merchant_name"], "expected the merchant to resolve"
        assert harris["suggested_category_id"] is not None

    def test_commit_then_recommit_is_a_no_op(self, client, category_id):
        preview = client.post("/api/imports/preview", data={"text": self.CSV}).json()
        rows = [
            {
                "occurred_on": r["occurred_on"],
                "raw_description": r["raw_description"],
                "amount": r["amount"],
                "category_id": r["suggested_category_id"] or category_id,
                "import_hash": r["import_hash"],
            }
            for r in preview["rows"]
        ]
        first = client.post("/api/imports/commit", json={"rows": rows}).json()
        assert first["created"] == 3
        assert first["skipped_duplicates"] == 0

        second = client.post("/api/imports/commit", json={"rows": rows}).json()
        assert second["created"] == 0
        assert second["skipped_duplicates"] == 3

    def test_parentheses_and_currency_symbols_parse(self, client):
        csv = "Date,Description,Amount\n2026-07-01,REFUNDED THING,($12.50)\n"
        body = client.post("/api/imports/preview", data={"text": csv}).json()
        assert body["rows"][0]["amount"] == "-12.50"

    def test_flip_sign_for_banks_that_export_purchases_negative(self, client):
        csv = "Date,Description,Amount\n2026-07-01,COFFEE,-4.75\n"
        body = client.post(
            "/api/imports/preview", data={"text": csv, "flip_sign": "true"}
        ).json()
        assert body["rows"][0]["amount"] == "4.75"

    def test_missing_amount_column_is_a_clear_error(self, client):
        body = client.post(
            "/api/imports/preview", data={"text": "Date,Description\n2026-01-01,x\n"}
        ).json()
        assert body["rows"] == []
        assert any("amount" in e for e in body["errors"])

    def test_empty_input_is_an_error_not_a_crash(self, client):
        body = client.post("/api/imports/preview", data={"text": "   "}).json()
        assert body["errors"]


class TestMerchantMerge:
    def test_merge_moves_transactions_and_removes_the_source(self, client, category_id):
        a = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-01",
                "raw_description": "ZZTEST ALPHA MARKET",
                "category_id": category_id,
                "amount": "5.00",
            },
        ).json()
        b = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-01",
                "raw_description": "ZZTEST ALPHA MART",
                "category_id": category_id,
                "amount": "6.00",
            },
        ).json()
        assert a["merchant_id"] != b["merchant_id"], "should start unmerged"

        r = client.post(
            f"/api/merchants/{b['merchant_id']}/merge",
            json={"into_id": a["merchant_id"]},
        )
        assert r.status_code == 200
        assert r.json()["transaction_count"] >= 2

        moved = client.get("/api/transactions?q=ZZTEST").json()
        assert {t["merchant_id"] for t in moved} == {a["merchant_id"]}

    def test_cannot_merge_into_itself(self, client, category_id):
        txn = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-01",
                "raw_description": "ZZTEST SOLO SHOP",
                "category_id": category_id,
                "amount": "5.00",
            },
        ).json()
        r = client.post(
            f"/api/merchants/{txn['merchant_id']}/merge",
            json={"into_id": txn["merchant_id"]},
        )
        assert r.status_code == 422


class TestMerchantSuggestions:
    def _group_names(self, client) -> list[list[str]]:
        return [
            [m["canonical_name"] for m in g["members"]]
            for g in client.get("/api/merchants/suggestions").json()
        ]

    def test_proposals_carry_the_raw_descriptors(self, client):
        """The names alone are not enough to decide; the descriptors are."""
        groups = client.get("/api/merchants/suggestions").json()
        assert groups, "expected proposals from the imported history"
        member = groups[0]["members"][0]
        assert member["examples"], "a proposal without descriptors cannot be judged"
        assert member["transaction_count"] > 0

    def test_saying_no_removes_the_group_permanently(self, client):
        before = self._group_names(client)
        assert before, "expected something to reject"
        target = before[0]

        r = client.post("/api/merchants/suggestions/reject", json={"names": target})
        assert r.status_code == 204

        after = self._group_names(client)
        assert target not in after, "a rejected group must not come back"

    def test_rejecting_is_idempotent(self, client):
        target = self._group_names(client)[0]
        for _ in range(2):
            assert (
                client.post(
                    "/api/merchants/suggestions/reject", json={"names": target}
                ).status_code
                == 204
            )

    def test_anchor_only_rejects_pairs_against_the_anchor(self, client):
        """After a partial merge, the leftovers differ from the survivor —
        but nothing has been decided about whether they differ from each other."""
        groups = self._group_names(client)
        target = next((g for g in groups if len(g) >= 3), None)
        if target is None:
            pytest.skip("no group with three or more members")

        anchor, *others = target
        client.post(
            "/api/merchants/suggestions/reject",
            json={"names": others, "anchor": anchor},
        )

        after = self._group_names(client)
        flat = [set(g) for g in after]
        assert not any({anchor, others[0]} <= g for g in flat), (
            "anchor should no longer be grouped with the rejected names"
        )
        assert any(set(others) <= g for g in flat), (
            "the others were never claimed to differ from each other"
        )
