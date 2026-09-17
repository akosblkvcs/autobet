"""Fixture names split into the two competitors they name."""

import pytest

from autobet.matching import two_sides


@pytest.mark.parametrize(
    "name",
    ["Montrose - FC Dundee 2", "Montrose vs. FC Dundee 2", "Montrose vs FC Dundee 2"],
)
def test_two_sides_splits_on_every_separator(name: str) -> None:
    """The feed writes a fixture three ways, and all three are one pairing."""
    assert two_sides(name) == ("Montrose", "FC Dundee 2")


@pytest.mark.parametrize("name", ["Tour de France", "Montrose - FC Dundee 2 - Ayr"])
def test_two_sides_rejects_anything_that_is_not_a_pair(name: str) -> None:
    """An event with no two sides, or more than two, has no pairing."""
    assert two_sides(name) is None
