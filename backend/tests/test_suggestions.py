"""Tests for merchant match proposals.

Every case is a real pair of merchant names from the imported history.

The rule is "same brand", not "same place" — so a proposal is a question, and
some correct proposals are answered no. The false-positive cases are the ones
that matter: proposing an obviously wrong merge trains the reviewer to click
through the queue without reading it.
"""

import pytest

from backend.suggestions import edit_distance_at_most_one, group_names, likely_same


class TestEditDistance:
    @pytest.mark.parametrize(
        ("a", "b"),
        [("teeter", "teater"), ("target", "targer"), ("rhino", "rhinot"), ("ab", "ab")],
    )
    def test_within_one(self, a, b):
        assert edit_distance_at_most_one(a, b)

    @pytest.mark.parametrize(
        ("a", "b"), [("parking", "park"), ("market", "mart"), ("eats", "trip")]
    )
    def test_beyond_one(self, a, b):
        assert not edit_distance_at_most_one(a, b)


class TestLikelySame:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Harris Teeter", "Harris Teater"),  # typo in the brand
            ("Target", "Targer"),
            ("Rhino Market", "Rhino Mart"),  # 'mart' is NOT a prefix of 'market'
            ("Rhino Market", "Rhino Market Deli"),
            ("Rhino", "Rhinot Market"),
            ("Kanna", "Kanna Cbd"),
            ("Netflix", "Netflix Com"),
            ("Apple", "Apple Com Bill"),
        ],
    )
    def test_proposes_same_brand(self, a, b):
        assert likely_same(a, b)
        assert likely_same(b, a), "the relation must be symmetric"

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Parking", "Park Road Books"),  # 'park' is 3 edits from 'parking'
            ("Harris Teeter", "Target"),
            ("Bar Crawl", "Bar Garden"),  # 'bar' is too generic to mean anything
            ("Cvs", "Cava"),  # too short to guess at
            ("Uber Eats", "Lyft Waitsave"),
        ],
    )
    def test_does_not_propose_different_brands(self, a, b):
        assert not likely_same(a, b)
        assert not likely_same(b, a)

    def test_shared_brand_is_proposed_even_when_it_is_not_one_place(self):
        """Uber Eats is Food and Drinks; Uber Trip is Transportation.

        Both are Uber, so both get proposed — and the reviewer ticks members
        individually rather than accepting the group wholesale. Proposing them
        is right; merging them would not be.
        """
        assert likely_same("Uber Eats", "Uber Trip")

    def test_empty_names_never_match(self):
        assert not likely_same("", "Target")
        assert not likely_same("   ", "")


class TestGrouping:
    def test_builds_connected_components(self):
        groups = group_names(
            ["Rhino Market", "Rhino Mart", "Rhino Market Deli", "Target"], set()
        )
        assert len(groups) == 1
        assert len(groups[0]) == 3
        assert "Target" not in groups[0]

    def test_a_rejected_pair_breaks_the_link(self):
        rejected = {tuple(sorted(("Rhino Market", "Rhino Mart")))}
        assert group_names(["Rhino Market", "Rhino Mart"], rejected) == [], (
            "saying no must remove the proposal, not hide it for a session"
        )

    def test_rejecting_one_pair_leaves_the_rest_of_the_group(self):
        """Rejecting a pair is not a claim about every other pair."""
        names = ["Kanna", "Kanna Cbd", "Kanna South End"]
        groups = group_names(names, {tuple(sorted(("Kanna", "Kanna Cbd")))})
        assert len(groups) == 1, "still linked through 'Kanna South End'"

    def test_singletons_are_not_proposed(self):
        assert group_names(["Target", "Netflix", "Spotify"], set()) == []
