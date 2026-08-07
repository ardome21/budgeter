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


class TestIdenticalRowsInOneFile:
    """Two identical charges in one export are two real purchases.

    They used to be a 500: the hash was taken over content alone, so the second
    row collided with the first against a unique index, and because sessions
    run with autoflush off the pre-check could not see the pending row either.
    The whole import was lost, not just the row.
    """

    CSV = (
        "Date,Description,Amount\n"
        "2029-03-04,SAME DAY COFFEE,3.50\n"
        "2029-03-04,SAME DAY COFFEE,3.50\n"
    )

    def _rows(self, preview, category_id):
        return [
            {
                "occurred_on": r["occurred_on"],
                "raw_description": r["raw_description"],
                "amount": r["amount"],
                "category_id": r["suggested_category_id"] or category_id,
                "import_hash": r["import_hash"],
            }
            for r in preview["rows"]
        ]

    def test_preview_gives_the_repeat_its_own_hash(self, client):
        rows = client.post("/api/imports/preview", data={"text": self.CSV}).json()[
            "rows"
        ]
        assert len({r["import_hash"] for r in rows}) == 2
        assert any("identical to row 2" in n for n in rows[1]["notes"])

    def test_both_rows_are_written(self, client, category_id):
        preview = client.post("/api/imports/preview", data={"text": self.CSV}).json()
        result = client.post(
            "/api/imports/commit", json={"rows": self._rows(preview, category_id)}
        )
        assert result.status_code == 200
        assert result.json() == {
            "created": 2,
            "skipped_duplicates": 0,
            "errors": [],
        }

    def test_redropping_the_same_file_still_skips_both(self, client, category_id):
        preview = client.post("/api/imports/preview", data={"text": self.CSV}).json()
        rows = self._rows(preview, category_id)
        client.post("/api/imports/commit", json={"rows": rows})

        again = client.post("/api/imports/preview", data={"text": self.CSV}).json()
        assert again["duplicate_count"] == 2
        second = client.post(
            "/api/imports/commit", json={"rows": self._rows(again, category_id)}
        ).json()
        assert second == {"created": 0, "skipped_duplicates": 2, "errors": []}

    def test_a_new_merchant_repeated_is_created_once(self, client, category_id):
        """The same crash one level down: the second row could not see the
        merchant the first had just created, and made a second one."""
        csv = (
            "Date,Description,Amount\n"
            "2029-03-05,ZZQQ NOVEL PLACE,8.00\n"
            "2029-03-05,ZZQQ NOVEL PLACE,8.00\n"
        )
        preview = client.post("/api/imports/preview", data={"text": csv}).json()
        result = client.post(
            "/api/imports/commit", json={"rows": self._rows(preview, category_id)}
        )
        assert result.status_code == 200
        assert result.json()["created"] == 2

        matches = client.get("/api/merchants?q=zzqq novel").json()
        assert matches["total"] == 1, "one shop, one merchant"


class TestNearDuplicates:
    """Same amount, near the same date, different wording.

    The hash cannot catch a bank export overlapping the imported workbook: the
    workbook's descriptions were typed by hand and never match a descriptor.
    """

    def test_a_matching_amount_nearby_is_flagged_but_stays_importable(
        self, client, category_id
    ):
        existing = client.post(
            "/api/transactions",
            json={
                "occurred_on": "2029-04-10",
                "raw_description": "Lunch",
                "amount": "21.34",
                "category_id": category_id,
            },
        ).json()

        csv = "Date,Description,Amount\n2029-04-11,SOME DELI CHARLOTTE NC,21.34\n"
        body = client.post("/api/imports/preview", data={"text": csv}).json()
        row = body["rows"][0]

        assert row["duplicate_of"] is None, "different wording cannot hash the same"
        assert body["near_duplicate_count"] == 1
        assert [m["id"] for m in row["near_duplicates"]] == [existing["id"]]
        assert row["near_duplicates"][0]["days_apart"] == 1

    def test_the_same_amount_far_away_is_not_flagged(self, client, category_id):
        client.post(
            "/api/transactions",
            json={
                "occurred_on": "2029-05-01",
                "raw_description": "Lunch",
                "amount": "77.11",
                "category_id": category_id,
            },
        )
        csv = "Date,Description,Amount\n2029-05-20,SOME DELI,77.11\n"
        body = client.post("/api/imports/preview", data={"text": csv}).json()
        assert body["rows"][0]["near_duplicates"] == []


class TestImportAccount:
    def _open_account(self, client):
        return next(a for a in client.get("/api/accounts").json() if not a["closed_on"])

    def test_committed_rows_carry_the_account(self, client, category_id):
        account = self._open_account(client)
        csv = "Date,Description,Amount\n2029-06-02,ACCOUNTED THING,15.00\n"
        preview = client.post(
            "/api/imports/preview",
            data={"text": csv, "account_id": str(account["id"])},
        ).json()
        assert preview["account_id"] == account["id"]

        client.post(
            "/api/imports/commit",
            json={
                "account_id": account["id"],
                "rows": [
                    {
                        "occurred_on": "2029-06-02",
                        "raw_description": "ACCOUNTED THING",
                        "amount": "15.00",
                        "category_id": category_id,
                        "import_hash": preview["rows"][0]["import_hash"],
                    }
                ],
            },
        )
        written = client.get(
            f"/api/transactions?account_id={account['id']}&q=ACCOUNTED"
        ).json()
        assert len(written) == 1
        assert written[0]["account_id"] == account["id"]
        assert (
            written[0]["account_name"] == f"{account['institution']} {account['name']}"
        )

    def test_the_same_charge_on_two_accounts_stays_two_rows(self, client, category_id):
        """A card export and a checking export can legitimately both show it."""
        accounts = [a for a in client.get("/api/accounts").json() if not a["closed_on"]]
        csv = "Date,Description,Amount\n2029-06-03,SHARED CHARGE,42.00\n"
        hashes = set()
        for account in accounts[:2]:
            preview = client.post(
                "/api/imports/preview",
                data={"text": csv, "account_id": str(account["id"])},
            ).json()
            hashes.add(preview["rows"][0]["import_hash"])
        assert len(hashes) == 2

    def test_a_closed_account_is_refused(self, client):
        closed = next(
            (a for a in client.get("/api/accounts").json() if a["closed_on"]), None
        )
        if closed is None:
            pytest.skip("no closed account in the database")
        response = client.post(
            "/api/imports/preview",
            data={
                "text": "Date,Description,Amount\n2029-01-01,X,1.00\n",
                "account_id": str(closed["id"]),
            },
        )
        assert response.status_code == 422
        assert "closed" in response.json()["detail"]

    def test_an_unknown_account_is_refused(self, client, category_id):
        response = client.post(
            "/api/imports/commit",
            json={
                "account_id": 99999999,
                "rows": [
                    {
                        "occurred_on": "2029-06-04",
                        "raw_description": "X",
                        "amount": "1.00",
                        "category_id": category_id,
                    }
                ],
            },
        )
        assert response.status_code == 422


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

    def test_a_merchant_stores_no_category_at_all(self, client, session):
        """The suggestion is history, and there is no stored default to drift
        from it. merchants.default_category_id recorded whichever transaction
        created the merchant and never moved — it disagreed with the merchant's
        own history for twelve of them — so it is gone."""
        from backend.models import Merchant

        assert not hasattr(Merchant, "default_category_id"), (
            "a stored category is a second copy of something derivable, and the "
            "two disagreed"
        )

        row = next(r for r in self._rows(client) if "RHINO" in r["raw_description"])
        merchant = (
            session.query(Merchant).filter_by(canonical_name=row["merchant_name"]).one()
        )
        assert merchant is not None
        assert row["suggested_category_id"] == row["category_options"][0]["id"]

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


class TestAccounts:
    def test_net_worth_points_are_chronological(self, client):
        points = client.get("/api/accounts/net-worth").json()["points"]
        assert points, "expected imported snapshots"
        dates = [p["as_of"] for p in points]
        assert dates == sorted(dates)

    def test_retirement_and_liquid_sum_to_net_worth(self, client):
        for p in client.get("/api/accounts/net-worth").json()["points"]:
            assert Decimal(p["retirement"]) + Decimal(p["liquid"]) == Decimal(
                p["net_worth"]
            ), f"split does not reconcile on {p['as_of']}"

    def test_liabilities_pull_net_worth_down(self, client):
        """The workbook's own Total row omitted the student loan and the credit
        card balance. Net worth that ignores what you owe is not net worth."""
        points = {
            p["as_of"]: p
            for p in client.get("/api/accounts/net-worth").json()["points"]
        }
        march = points.get("2024-03-02")
        if march is None:
            pytest.skip("March 2024 snapshot not present")
        # The sheet reported 53,742.84 by leaving out a $6,000 loan.
        assert Decimal(march["net_worth"]) == Decimal("47742.84")

    def test_money_crosses_as_strings(self, client):
        p = client.get("/api/accounts/net-worth").json()["points"][0]
        assert all(isinstance(p[k], str) for k in ("net_worth", "retirement", "liquid"))

    def test_accounts_report_latest_and_change(self, client):
        rows = client.get("/api/accounts").json()
        assert rows
        multi = [r for r in rows if r["snapshot_count"] > 1]
        assert multi, "expected an account with history"
        assert multi[0]["change"] is not None
        single = [r for r in rows if r["snapshot_count"] == 1]
        if single:
            assert single[0]["change"] is None, "one snapshot cannot show a change"

    def test_recording_a_balance_twice_on_one_date_corrects_it(self, client):
        acct = client.post(
            "/api/accounts",
            json={"institution": "ZZBank", "name": "ZZTest", "is_retirement": False},
        ).json()

        first = client.put(
            f"/api/accounts/{acct['id']}/balances",
            json={"as_of": "2026-01-15", "balance": "100.00"},
        )
        assert first.status_code == 200

        second = client.put(
            f"/api/accounts/{acct['id']}/balances",
            json={"as_of": "2026-01-15", "balance": "250.00"},
        )
        assert second.status_code == 200
        assert second.json()["balance"] == "250.00"

        balances = client.get(f"/api/accounts/{acct['id']}/balances").json()
        assert len(balances) == 1, "a second reading on one day is a correction"

    def test_duplicate_account_is_a_conflict(self, client):
        body = {"institution": "ZZDupe", "name": "ZZOnly", "is_retirement": False}
        assert client.post("/api/accounts", json=body).status_code == 201
        assert client.post("/api/accounts", json=body).status_code == 409

    def test_renaming_onto_an_existing_pair_is_a_conflict(self, client):
        a = client.post(
            "/api/accounts",
            json={"institution": "ZZC", "name": "One", "is_retirement": False},
        ).json()
        client.post(
            "/api/accounts",
            json={"institution": "ZZC", "name": "Two", "is_retirement": False},
        )
        r = client.patch(f"/api/accounts/{a['id']}", json={"name": "Two"})
        assert r.status_code == 409

    def test_deleting_an_account_takes_its_snapshots(self, client):
        acct = client.post(
            "/api/accounts",
            json={"institution": "ZZGone", "name": "ZZGone", "is_retirement": False},
        ).json()
        client.put(
            f"/api/accounts/{acct['id']}/balances",
            json={"as_of": "2026-01-15", "balance": "5.00"},
        )
        assert client.delete(f"/api/accounts/{acct['id']}").status_code == 204
        assert client.get(f"/api/accounts/{acct['id']}/balances").status_code == 404

    def test_unknown_account_is_404(self, client):
        assert (
            client.put(
                "/api/accounts/999999/balances",
                json={"as_of": "2026-01-15", "balance": "1.00"},
            ).status_code
            == 404
        )


class TestStaleAndClosedAccounts:
    """A balance from two years ago is history, not a position.

    Shown under a column headed "Latest", a settled loan reads as money still
    owed — which is how a debt that was paid off long ago keeps haunting a net
    worth screen.
    """

    def test_an_account_behind_the_newest_snapshot_is_flagged_stale(self, client):
        rows = client.get("/api/accounts").json()
        stale = [r for r in rows if r["is_stale"]]
        current = [r for r in rows if not r["is_stale"]]
        assert current, "expected accounts read on the latest date"
        for r in current:
            assert r["days_behind"] in (0, None)
        for r in stale:
            assert r["days_behind"] and r["days_behind"] > 0

    def test_stale_accounts_are_absent_from_the_latest_net_worth(self, client):
        rows = client.get("/api/accounts").json()
        points = client.get("/api/accounts/net-worth").json()["points"]
        latest = points[-1]
        reported = sum(1 for r in rows if not r["is_stale"] and r["latest_as_of"])
        assert latest["accounts_reported"] == reported, (
            "a stale balance must not be counted as a current position"
        )

    def test_closing_keeps_the_history(self, client):
        acct = client.post(
            "/api/accounts",
            json={"institution": "ZZLoan", "name": "ZZPaidOff", "is_retirement": False},
        ).json()
        client.put(
            f"/api/accounts/{acct['id']}/balances",
            json={"as_of": "2024-03-02", "balance": "-6000.00"},
        )

        closed = client.patch(
            f"/api/accounts/{acct['id']}", json={"closed_on": "2026-08-07"}
        )
        assert closed.status_code == 200
        assert closed.json()["closed_on"] == "2026-08-07"
        assert closed.json()["latest_balance"] == "-6000.00", "history survives"

        balances = client.get(f"/api/accounts/{acct['id']}/balances").json()
        assert len(balances) == 1

    def test_closing_does_not_move_net_worth(self, client):
        """Closing is a statement about now, not a rewrite of what happened."""
        before = [
            (p["as_of"], p["net_worth"])
            for p in client.get("/api/accounts/net-worth").json()["points"]
        ]
        target = next(r for r in client.get("/api/accounts").json() if r["is_stale"])
        client.patch(f"/api/accounts/{target['id']}", json={"closed_on": "2026-08-07"})
        after = [
            (p["as_of"], p["net_worth"])
            for p in client.get("/api/accounts/net-worth").json()["points"]
        ]
        assert before == after

    def test_reopening_clears_the_closed_date(self, client):
        acct = client.post(
            "/api/accounts",
            json={"institution": "ZZReopen", "name": "ZZAcct", "is_retirement": False},
        ).json()
        client.patch(f"/api/accounts/{acct['id']}", json={"closed_on": "2026-08-07"})
        r = client.patch(f"/api/accounts/{acct['id']}", json={"closed_on": None})
        assert r.status_code == 200
        assert r.json()["closed_on"] is None


class TestBudgetEditing:
    def _cat_ids(self, client) -> list[int]:
        return [c["id"] for c in client.get("/api/categories").json()]

    def test_set_and_read_back_a_budget(self, client):
        ids = self._cat_ids(client)
        r = client.put(
            "/api/periods/2027/3/allocations",
            json={
                "allocations": [
                    {"category_id": ids[0], "amount": "500.00"},
                    {"category_id": ids[1], "amount": "250.50"},
                ]
            },
        )
        assert r.status_code == 200
        assert r.json()["total"] == "750.50"
        assert client.get("/api/periods/2027/3/allocations").json()["total"] == "750.50"

    def test_put_replaces_rather_than_merges(self, client):
        ids = self._cat_ids(client)
        client.put(
            "/api/periods/2027/4/allocations",
            json={"allocations": [{"category_id": ids[0], "amount": "100.00"}]},
        )
        r = client.put(
            "/api/periods/2027/4/allocations",
            json={"allocations": [{"category_id": ids[1], "amount": "70.00"}]},
        )
        assert r.json()["total"] == "70.00", "a budget is one decision, not a merge"
        assert len(r.json()["allocations"]) == 1

    def test_zero_allocations_are_dropped(self, client):
        ids = self._cat_ids(client)
        r = client.put(
            "/api/periods/2027/5/allocations",
            json={
                "allocations": [
                    {"category_id": ids[0], "amount": "0"},
                    {"category_id": ids[1], "amount": "10.00"},
                ]
            },
        )
        assert len(r.json()["allocations"]) == 1

    def test_copying_last_month(self, client):
        ids = self._cat_ids(client)
        client.put(
            "/api/periods/2027/6/allocations",
            json={"allocations": [{"category_id": ids[0], "amount": "800.00"}]},
        )
        r = client.post(
            "/api/periods/2027/7/allocations/copy?from_year=2027&from_month=6"
        )
        assert r.status_code == 200
        assert r.json()["total"] == "800.00"

    def test_copying_from_an_empty_month_is_rejected(self, client):
        r = client.post(
            "/api/periods/2027/8/allocations/copy?from_year=1999&from_month=1"
        )
        assert r.status_code == 404

    def test_unknown_category_is_rejected(self, client):
        r = client.put(
            "/api/periods/2027/9/allocations",
            json={"allocations": [{"category_id": 999999, "amount": "1.00"}]},
        )
        assert r.status_code == 422

    def test_budget_shows_up_on_the_month_summary(self, client):
        """Editing the budget must move the same figure the Month screen reads."""
        ids = self._cat_ids(client)
        client.put(
            "/api/periods/2026/7/allocations",
            json={"allocations": [{"category_id": ids[0], "amount": "123.45"}]},
        )
        summary = client.get("/api/periods/2026/7/summary").json()
        assert summary["allocated_total"] == "123.45"


class TestFixedCostEditing:
    def test_components_are_not_counted_beside_their_parent(self, client):
        rows = client.get("/api/fixed-costs").json()
        assert rows
        rent = next((r for r in rows if r["components"]), None)
        if rent is None:
            pytest.skip("no fixed cost with a breakdown")
        component_sum = sum(Decimal(c["amount"]) for c in rent["components"])
        assert component_sum == Decimal(rent["amount"]), (
            "a breakdown must equal the bill it breaks down"
        )
        assert all(r["parent_id"] is None for r in rows), (
            "components must not appear at the top level, or the total doubles"
        )

    def test_overview_counts_each_commitment_once(self, client):
        """The overview's fixed-cost total must be the fixed-cost list's total.

        These are the same money reached by two routes, and they disagreed:
        the overview summed every effective row while the list returned only
        top-level ones, so the rent breakdown was counted twice — 3586.27
        against a real 2032.90, with disposable income short by the whole rent
        charge. Both the total and the per-category groups are checked, because
        the category groups are what the double-count was visible in.
        """
        costs = client.get("/api/fixed-costs").json()
        overview = client.get("/api/overview").json()

        assert Decimal(overview["fixed_costs"]) == sum(
            Decimal(c["amount"]) for c in costs
        ), "the overview and the fixed-cost list must agree on the total"

        by_category: dict[str, Decimal] = {}
        for cost in costs:
            by_category[cost["category"]] = by_category.get(
                cost["category"], Decimal("0.00")
            ) + Decimal(cost["amount"])
        assert {
            g["category"]: Decimal(g["amount"]) for g in overview["fixed_by_category"]
        } == by_category

        # A component's description must not appear beside its parent's.
        lines = [
            line for group in overview["fixed_by_category"] for line in group["lines"]
        ]
        components = [c["description"] for cost in costs for c in cost["components"]]
        assert not (set(lines) & set(components)), (
            "a breakdown line is part of its parent, not a commitment beside it"
        )

    def test_changing_the_amount_opens_a_new_row_and_ends_the_old(self, client):
        cats = client.get("/api/categories").json()
        created = client.post(
            "/api/fixed-costs",
            json={
                "description": "ZZTest Subscription",
                "amount": "10.00",
                "category_id": cats[0]["id"],
                "effective_from": "2020-01-01",
            },
        ).json()
        updated = client.patch(
            f"/api/fixed-costs/{created['id']}", json={"amount": "12.00"}
        ).json()
        assert updated["id"] != created["id"], "history is kept, not overwritten"
        assert updated["amount"] == "12.00"

        ended = [
            r
            for r in client.get("/api/fixed-costs?include_ended=true").json()
            if r["id"] == created["id"]
        ]
        assert ended and ended[0]["effective_to"] is not None

    def test_renaming_does_not_fork_history(self, client):
        cats = client.get("/api/categories").json()
        created = client.post(
            "/api/fixed-costs",
            json={
                "description": "ZZTypo",
                "amount": "5.00",
                "category_id": cats[0]["id"],
                "effective_from": "2020-01-01",
            },
        ).json()
        updated = client.patch(
            f"/api/fixed-costs/{created['id']}", json={"description": "ZZFixed"}
        ).json()
        assert updated["id"] == created["id"], "a typo fix is not a price change"

    def test_deleting_ends_rather_than_erases(self, client):
        cats = client.get("/api/categories").json()
        created = client.post(
            "/api/fixed-costs",
            json={
                "description": "ZZCancelled",
                "amount": "9.99",
                "category_id": cats[0]["id"],
            },
        ).json()
        assert client.delete(f"/api/fixed-costs/{created['id']}").status_code == 204
        still_there = [
            r
            for r in client.get("/api/fixed-costs?include_ended=true").json()
            if r["id"] == created["id"]
        ]
        assert still_there, "a cancelled subscription still explains last month"


class TestReconciliation:
    def test_matched_rows_carry_a_drift(self, client):
        body = client.get("/api/periods/2026/7/reconcile").json()
        matched = [r for r in body["rows"] if r["actual"] is not None]
        assert matched, "expected some commitments to match a merchant"
        for r in matched:
            assert Decimal(r["drift"]) == Decimal(r["actual"]) - Decimal(r["expected"])

    def test_unmatched_rows_say_so_instead_of_guessing(self, client):
        """Falling back to the category total made Energy and Phone both report
        192.74 — the whole Utilities category, twice. A blank is more useful."""
        body = client.get("/api/periods/2026/7/reconcile").json()
        for r in body["rows"]:
            if r["note"] == "not linked to a merchant yet":
                assert r["actual"] is None
                assert r["drift"] is None

    def test_unmatched_rows_offer_candidates(self, client, category_id):
        """An unmatched commitment must offer a way out of being unmatched.

        This creates the unlinked row it needs rather than hunting for one in
        the data. It used to search, and passed only because seven real
        commitments were pointing at merchants nobody bills under; once those
        were linked there was nothing left to find and the assertion started
        proving nothing.
        """
        merchants = client.get("/api/merchants?limit=1").json()["rows"]
        assert merchants, "expected merchants in the database"
        name = merchants[0]["canonical_name"]

        created = client.post(
            "/api/fixed-costs",
            json={
                "description": f"ZZ {name} Membership",
                "amount": "5.00",
                "category_id": category_id,
                "effective_from": "2020-01-01",
            },
        ).json()

        row = next(
            r
            for r in client.get("/api/periods/2026/7/reconcile").json()["rows"]
            if r["fixed_cost_id"] == created["id"]
        )
        assert row["note"] == "not linked to a merchant yet"
        assert row["actual"] is None
        assert row["suggestions"], (
            "an unmatched commitment with no suggestion is a dead end"
        )

    def test_linking_a_merchant_makes_the_row_reconcile(self, client, category_id):
        """Accepting a suggested candidate is what closes an unmatched row.

        Like the test above, this builds its own unlinked commitment. It used
        to look for one and skipped when it found none, so once the real
        commitments were linked it stopped running at all — a test that
        silently stops testing is worse than one that fails.
        """
        merchant = client.get("/api/merchants?limit=1").json()["rows"][0]
        created = client.post(
            "/api/fixed-costs",
            json={
                "description": f"ZZ {merchant['canonical_name']} Membership",
                "amount": "5.00",
                "category_id": category_id,
                "effective_from": "2020-01-01",
            },
        ).json()

        body = client.get("/api/periods/2026/7/reconcile").json()
        target = next(r for r in body["rows"] if r["fixed_cost_id"] == created["id"])
        assert target["note"] == "not linked to a merchant yet"
        assert target["suggestions"], "expected a candidate to accept"

        merchant_id = target["suggestions"][0][0]
        client.patch(
            f"/api/fixed-costs/{target['fixed_cost_id']}",
            json={"merchant_id": merchant_id},
        )
        after = client.get("/api/periods/2026/7/reconcile").json()
        row = next(
            r for r in after["rows"] if r["fixed_cost_id"] == target["fixed_cost_id"]
        )
        assert row["merchant"] is not None
        assert row["actual"] is not None

    def test_expected_total_matches_the_fixed_cost_total(self, client):
        body = client.get("/api/periods/2026/7/reconcile").json()
        costs = client.get("/api/fixed-costs").json()
        assert Decimal(body["expected_total"]) == sum(
            Decimal(c["amount"]) for c in costs
        )


class TestRecurringBackfill:
    """`recurring_candidates`, which the backfill script drives."""

    def test_an_explicit_merchant_link_is_honoured(self, session, category_id):
        """The link must win over the description, as it does in reconcile.

        Reading only the description found nothing for rent, phone or the
        paper — the commitments whose bill is named differently from their
        charge, which is to say the ones that matter. Those are exactly the
        rows a committed-vs-flexible split is wrong without.
        """
        from datetime import date

        from backend.models import (
            BudgetPeriod,
            FixedCost,
            Merchant,
            Transaction,
            TransactionSource,
        )
        from backend.reconcile import recurring_candidates

        merchant = Merchant(canonical_name="ZZ Test Landlord Card")
        period = session.query(BudgetPeriod).first()
        if period is None:
            pytest.skip("no periods in the database — run the importer first")
        session.add(merchant)
        session.flush()

        txn = Transaction(
            period_id=period.id,
            raw_description="ZZ LANDLORD CARD HOUSING",
            merchant_id=merchant.id,
            category_id=category_id,
            amount=Decimal("1000.00"),
            is_recurring=False,
            source=TransactionSource.CSV,
        )
        # The name nobody bills under — this is the whole point.
        cost = FixedCost(
            description="ZZ Test Rent",
            amount=Decimal("1000.00"),
            category_id=category_id,
            effective_from=date(2020, 1, 1),
            merchant_id=merchant.id,
        )
        session.add_all([txn, cost])
        session.flush()

        found = {t[0] for t in recurring_candidates(session)}
        assert txn.id in found, (
            "a transaction charged by a linked merchant must be a candidate "
            "even though the commitment's description matches nothing"
        )

    def test_a_breakdown_line_is_not_treated_as_a_payee(self, session, category_id):
        """Components are invoice lines, not merchants.

        'Internet' and 'Valet Trash' are what the rent is made of. Matching a
        merchant against them would mark someone else's transactions recurring
        and attribute them to a line item.
        """
        from datetime import date

        from backend.models import (
            BudgetPeriod,
            FixedCost,
            Merchant,
            Transaction,
            TransactionSource,
        )
        from backend.reconcile import recurring_candidates

        period = session.query(BudgetPeriod).first()
        if period is None:
            pytest.skip("no periods in the database — run the importer first")

        merchant = Merchant(canonical_name="Zzcomponent")
        session.add(merchant)
        session.flush()

        parent = FixedCost(
            description="ZZ Parent Bill",
            amount=Decimal("50.00"),
            category_id=category_id,
            effective_from=date(2020, 1, 1),
        )
        session.add(parent)
        session.flush()
        session.add(
            FixedCost(
                description="Zzcomponent",
                amount=Decimal("50.00"),
                category_id=category_id,
                effective_from=date(2020, 1, 1),
                parent_id=parent.id,
            )
        )
        txn = Transaction(
            period_id=period.id,
            raw_description="ZZCOMPONENT SOMETHING",
            merchant_id=merchant.id,
            category_id=category_id,
            amount=Decimal("50.00"),
            is_recurring=False,
            source=TransactionSource.CSV,
        )
        session.add(txn)
        session.flush()

        assert txn.id not in {t[0] for t in recurring_candidates(session)}
