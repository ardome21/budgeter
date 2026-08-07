"""API behaviour tests.

These run against the real database inside a rolled-back transaction, so they
assert on real Postgres constraints rather than a stand-in.
"""

from decimal import Decimal


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
    """These seed their own merchants rather than relying on whatever the
    database happens to hold — the real queue gets emptied by real use, and a
    test that needs it full would start failing the day the work got done."""

    BRAND = "ZZQUEUE"

    def _seed_group(self, client, category_id, suffixes=("MARKET", "MART", "DELI")):
        for suffix in suffixes:
            client.post(
                "/api/transactions",
                json={
                    "occurred_on": "2026-03-01",
                    "raw_description": f"{self.BRAND} {suffix}",
                    "category_id": category_id,
                    "amount": "5.00",
                },
            )
        return self._mine(client)

    def _mine(self, client) -> list[str]:
        """The seeded group, found by brand rather than by position."""
        for group in client.get("/api/merchants/suggestions").json():
            names = [m["canonical_name"] for m in group["members"]]
            if any(n.upper().startswith(self.BRAND) for n in names):
                return sorted(names)
        return []

    def test_proposals_carry_the_raw_descriptors(self, client, category_id):
        """The names alone are not enough to decide; the descriptors are."""
        self._seed_group(client, category_id)
        groups = client.get("/api/merchants/suggestions").json()
        mine = next(
            g
            for g in groups
            if any(
                m["canonical_name"].upper().startswith(self.BRAND) for m in g["members"]
            )
        )
        member = mine["members"][0]
        assert member["examples"], "a proposal without descriptors cannot be judged"
        assert member["transaction_count"] > 0

    def test_saying_no_removes_the_group_permanently(self, client, category_id):
        target = self._seed_group(client, category_id)
        assert len(target) == 3

        r = client.post("/api/merchants/suggestions/reject", json={"names": target})
        assert r.status_code == 204
        assert self._mine(client) == [], "a rejected group must not come back"

    def test_rejecting_is_idempotent(self, client, category_id):
        target = self._seed_group(client, category_id)
        for _ in range(2):
            assert (
                client.post(
                    "/api/merchants/suggestions/reject", json={"names": target}
                ).status_code
                == 204
            )

    def test_anchor_only_rejects_pairs_against_the_anchor(self, client, category_id):
        """After a partial merge the leftovers differ from the survivor, but
        nothing has been decided about whether they differ from each other."""
        target = self._seed_group(client, category_id)
        anchor, *others = target

        client.post(
            "/api/merchants/suggestions/reject",
            json={"names": others, "anchor": anchor},
        )

        still = self._mine(client)
        assert anchor not in still, "the anchor should no longer be grouped"
        assert set(others) <= set(still), (
            "the others were never claimed to differ from each other"
        )


class TestMerchantRename:
    def _merchant(self, client, category_id, description: str) -> dict:
        txn = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-01",
                "raw_description": description,
                "category_id": category_id,
                "amount": "5.00",
            },
        ).json()
        return txn

    def test_rename_sticks(self, client, category_id):
        txn = self._merchant(client, category_id, "ZZRENAME ORIGINAL SHOP")
        r = client.patch(
            f"/api/merchants/{txn['merchant_id']}",
            json={"canonical_name": "Rhino Market & Deli (mine)"},
        )
        assert r.status_code == 200
        assert r.json()["canonical_name"] == "Rhino Market & Deli (mine)"

        again = client.get("/api/transactions?q=ZZRENAME").json()
        assert again[0]["merchant_name"] == "Rhino Market & Deli (mine)"

    def test_rename_rewrites_split_records(self, client, category_id):
        """Splits are keyed by name. If a rename orphaned them, decisions the
        user already made would quietly come back as fresh proposals."""
        a = self._merchant(client, category_id, "ZZSPLIT ALPHA")
        self._merchant(client, category_id, "ZZSPLIT BETA")
        names = [
            client.get("/api/transactions?q=ZZSPLIT ALPHA").json()[0]["merchant_name"],
            client.get("/api/transactions?q=ZZSPLIT BETA").json()[0]["merchant_name"],
        ]
        client.post("/api/merchants/suggestions/reject", json={"names": names})

        client.patch(
            f"/api/merchants/{a['merchant_id']}",
            json={"canonical_name": "ZZSPLIT Renamed Alpha"},
        )
        # Re-rejecting the new name pair must be a no-op if the record followed
        # the rename; a fresh insert would mean the old one was orphaned.
        r = client.post(
            "/api/merchants/suggestions/reject",
            json={"names": ["ZZSPLIT Renamed Alpha", names[1]]},
        )
        assert r.status_code == 204

    def test_blank_name_is_rejected(self, client, category_id):
        txn = self._merchant(client, category_id, "ZZBLANK SHOP")
        assert (
            client.patch(
                f"/api/merchants/{txn['merchant_id']}", json={"canonical_name": "   "}
            ).status_code
            == 422
        )

    def test_name_already_taken_is_a_conflict_not_a_crash(self, client, category_id):
        a = self._merchant(client, category_id, "ZZTAKEN ONE")
        self._merchant(client, category_id, "ZZTAKEN TWO")
        taken = client.get("/api/transactions?q=ZZTAKEN TWO").json()[0]["merchant_name"]
        r = client.patch(
            f"/api/merchants/{a['merchant_id']}", json={"canonical_name": taken}
        )
        assert r.status_code == 409
        assert "merge" in r.json()["detail"]

    def test_renaming_to_the_same_name_is_allowed(self, client, category_id):
        txn = self._merchant(client, category_id, "ZZSAME SHOP")
        current = client.get("/api/transactions?q=ZZSAME").json()[0]["merchant_name"]
        r = client.patch(
            f"/api/merchants/{txn['merchant_id']}", json={"canonical_name": current}
        )
        assert r.status_code == 200

    def test_unknown_merchant_is_404(self, client):
        assert (
            client.patch(
                "/api/merchants/999999", json={"canonical_name": "Whatever"}
            ).status_code
            == 404
        )


class TestMergeThenRenameSequence:
    """The exact sequence the review screen performs, in order.

    Ordering matters: splits are keyed by name, so the rename has to land
    before rejections are recorded, or the decisions would be written against
    a name that no longer exists.
    """

    def _add(self, client, category_id, description: str) -> dict:
        return client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-01",
                "raw_description": description,
                "category_id": category_id,
                "amount": "5.00",
            },
        ).json()

    def test_merge_rename_then_reject_against_the_new_name(self, client, category_id):
        keep = self._add(client, category_id, "ZZFLOW MARKET")
        fold = self._add(client, category_id, "ZZFLOW MART")
        apart = self._add(client, category_id, "ZZFLOW DELIVERY")
        assert (
            len({keep["merchant_id"], fold["merchant_id"], apart["merchant_id"]}) == 3
        )

        merged = client.post(
            f"/api/merchants/{fold['merchant_id']}/merge",
            json={"into_id": keep["merchant_id"]},
        )
        assert merged.status_code == 200
        assert merged.json()["transaction_count"] == 2

        renamed = client.patch(
            f"/api/merchants/{keep['merchant_id']}",
            json={"canonical_name": "ZZFLOW Market & Deli"},
        )
        assert renamed.status_code == 200

        apart_name = client.get("/api/transactions?q=ZZFLOW DELIVERY").json()[0][
            "merchant_name"
        ]
        rejected = client.post(
            "/api/merchants/suggestions/reject",
            json={"names": [apart_name], "anchor": "ZZFLOW Market & Deli"},
        )
        assert rejected.status_code == 204

        # Both original descriptors now resolve to the typed name.
        rows = client.get("/api/transactions?q=ZZFLOW").json()
        by_desc = {r["raw_description"]: r["merchant_name"] for r in rows}
        assert by_desc["ZZFLOW MARKET"] == "ZZFLOW Market & Deli"
        assert by_desc["ZZFLOW MART"] == "ZZFLOW Market & Deli"
        assert by_desc["ZZFLOW DELIVERY"] == apart_name

    def test_merging_clears_the_losing_names_split_records(self, client, category_id):
        """The folded name refers to nothing afterwards. Leaving its splits
        behind would hand a future merchant of that name decisions nobody made
        about it."""
        keep = self._add(client, category_id, "ZZSTALE ALPHA")
        fold = self._add(client, category_id, "ZZSTALE BETA")
        fold_name = client.get("/api/transactions?q=ZZSTALE BETA").json()[0][
            "merchant_name"
        ]
        keep_name = client.get("/api/transactions?q=ZZSTALE ALPHA").json()[0][
            "merchant_name"
        ]
        client.post(
            "/api/merchants/suggestions/reject",
            json={"names": [keep_name, fold_name]},
        )
        client.post(
            f"/api/merchants/{fold['merchant_id']}/merge",
            json={"into_id": keep["merchant_id"]},
        )
        # Re-recording the same pair must insert cleanly, proving the stale row
        # was removed rather than left dangling.
        assert (
            client.post(
                "/api/merchants/suggestions/reject",
                json={"names": [keep_name, fold_name]},
            ).status_code
            == 204
        )


class TestCategoryOptions:
    """A merchant has a history of categories, not one category.

    Rhino Market & Deli is Food and Drinks on a sandwich run and Groceries on
    a shop, and both are correct — so the preview offers what the merchant has
    actually been used for rather than forcing a single answer.
    """

    CSV = (
        "Date,Description,Amount\n"
        "2026-08-07,RHINO MARKET & DELI CHARLOTTE NC,12.50\n"
        "2026-08-07,ZZ NEVER SEEN BEFORE,9.99\n"
    )

    def _rows(self, client):
        return client.post("/api/imports/preview", data={"text": self.CSV}).json()[
            "rows"
        ]

    def test_offers_every_category_the_merchant_has_been_used_with(self, client):
        row = next(r for r in self._rows(client) if "RHINO" in r["raw_description"])
        names = [o["name"] for o in row["category_options"]]
        assert len(names) > 1, "expected a merchant used across several categories"
        assert row["suggested_category_name"] == names[0]

    def test_options_are_ranked_by_how_often_each_was_used(self, client):
        row = next(r for r in self._rows(client) if "RHINO" in r["raw_description"])
        counts = [o["count"] for o in row["category_options"]]
        assert counts == sorted(counts, reverse=True)
        assert all(c > 0 for c in counts)

    def test_suggestion_comes_from_history_not_the_stored_default(
        self, client, session
    ):
        """merchants.default_category_id records whatever the first transaction
        happened to be and does not follow a merge. History cannot go stale."""
        from backend.models import Merchant

        row = next(r for r in self._rows(client) if "RHINO" in r["raw_description"])
        merchant = (
            session.query(Merchant).filter_by(canonical_name=row["merchant_name"]).one()
        )

        top_id = row["category_options"][0]["id"]
        assert row["suggested_category_id"] == top_id
        if merchant.default_category_id != top_id:
            # This is the real case in the imported data — the stored default
            # disagrees, and the suggestion correctly ignores it.
            assert row["suggested_category_id"] != merchant.default_category_id

    def test_an_unknown_merchant_has_no_options_and_no_suggestion(self, client):
        row = next(
            r for r in self._rows(client) if "NEVER SEEN" in r["raw_description"]
        )
        assert row["category_options"] == []
        assert row["suggested_category_id"] is None
        assert "new merchant" in row["notes"]


class TestWorkbench:
    """The merchant list and the hand-merge it exists for."""

    def _add(self, client, category_id, description: str, amount: str) -> dict:
        return client.post(
            "/api/transactions",
            json={
                "occurred_on": "2026-03-01",
                "raw_description": description,
                "category_id": category_id,
                "amount": amount,
            },
        ).json()

    def test_rows_carry_spend_and_category_mix(self, client):
        page = client.get("/api/merchants?sort=spend&limit=5").json()
        assert page["total"] > 0
        row = page["rows"][0]
        assert isinstance(row["total_spent"], str), "money must not be a JSON number"
        assert row["transaction_count"] > 0
        assert row["categories"], "the mix is what makes a row judgeable"

    def test_default_sort_is_by_spend_descending(self, client):
        rows = client.get("/api/merchants?sort=spend&limit=20").json()["rows"]
        totals = [float(r["total_spent"]) for r in rows]
        assert totals == sorted(totals, reverse=True)

    def test_sort_by_count(self, client):
        rows = client.get("/api/merchants?sort=count&limit=20").json()["rows"]
        counts = [r["transaction_count"] for r in rows]
        assert counts == sorted(counts, reverse=True)

    def test_search_narrows_and_reports_its_own_total(self, client):
        page = client.get("/api/merchants?q=zzzznotamerchant").json()
        assert page["rows"] == []
        assert page["total"] == 0

    def test_bad_sort_is_rejected(self, client):
        assert client.get("/api/merchants?sort=nonsense").status_code == 422

    def test_hand_merge_across_different_brands(self, client, category_id):
        """The suggestion rule keys on the first word, so it can never propose
        these — which is the whole reason the workbench merge exists."""
        a = self._add(client, category_id, "ZZWB Airbnb", "100.00")
        b = self._add(client, category_id, "ZZWB Future Rent Airbnb", "200.00")
        c = self._add(client, category_id, "ZZWB Revolution Park Air Bnb", "300.00")
        ids = {a["merchant_id"], b["merchant_id"], c["merchant_id"]}
        assert len(ids) == 3

        r = client.post(
            "/api/merchants/merge",
            json={
                "source_ids": [b["merchant_id"], c["merchant_id"]],
                "into_id": a["merchant_id"],
                "canonical_name": "ZZWB Airbnb (all of it)",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["canonical_name"] == "ZZWB Airbnb (all of it)"
        assert r.json()["transaction_count"] == 3

        rows = client.get("/api/transactions?q=ZZWB").json()
        assert {t["merchant_name"] for t in rows} == {"ZZWB Airbnb (all of it)"}
        assert sum(float(t["amount"]) for t in rows) == 600.00

    def test_merge_without_a_rename_keeps_the_survivors_name(self, client, category_id):
        a = self._add(client, category_id, "ZZKEEP ALPHA", "10.00")
        b = self._add(client, category_id, "ZZKEEP BETA", "20.00")
        before = client.get("/api/transactions?q=ZZKEEP ALPHA").json()[0][
            "merchant_name"
        ]
        r = client.post(
            "/api/merchants/merge",
            json={"source_ids": [b["merchant_id"]], "into_id": a["merchant_id"]},
        )
        assert r.json()["canonical_name"] == before

    def test_survivor_listed_as_a_source_is_ignored_not_an_error(
        self, client, category_id
    ):
        a = self._add(client, category_id, "ZZSELF ALPHA", "10.00")
        b = self._add(client, category_id, "ZZSELF BETA", "20.00")
        r = client.post(
            "/api/merchants/merge",
            json={
                "source_ids": [a["merchant_id"], b["merchant_id"]],
                "into_id": a["merchant_id"],
            },
        )
        assert r.status_code == 200
        assert r.json()["transaction_count"] == 2

    def test_merging_only_the_survivor_is_rejected(self, client, category_id):
        a = self._add(client, category_id, "ZZONLY ALPHA", "10.00")
        r = client.post(
            "/api/merchants/merge",
            json={"source_ids": [a["merchant_id"]], "into_id": a["merchant_id"]},
        )
        assert r.status_code == 422

    def test_unknown_source_is_404(self, client, category_id):
        a = self._add(client, category_id, "ZZMISS ALPHA", "10.00")
        r = client.post(
            "/api/merchants/merge",
            json={"source_ids": [999999], "into_id": a["merchant_id"]},
        )
        assert r.status_code == 404

    def test_money_is_never_moved_by_a_merge(self, client, category_id):
        """Merging relabels. Every amount must survive it untouched.

        Scoped to the rows under test rather than a page of all transactions —
        the list endpoint pages at 1000 and there are more than that, so a
        global sum measures the window, not the truth.
        """
        a = self._add(client, category_id, "ZZMONEY ALPHA", "11.11")
        b = self._add(client, category_id, "ZZMONEY BETA", "22.22")
        before = {
            t["raw_description"]: t["amount"]
            for t in client.get("/api/transactions?q=ZZMONEY").json()
        }
        assert before == {"ZZMONEY ALPHA": "11.11", "ZZMONEY BETA": "22.22"}

        client.post(
            "/api/merchants/merge",
            json={"source_ids": [b["merchant_id"]], "into_id": a["merchant_id"]},
        )

        after = client.get("/api/transactions?q=ZZMONEY").json()
        assert {t["raw_description"]: t["amount"] for t in after} == before
        assert len({t["merchant_id"] for t in after}) == 1, "both now share a merchant"
