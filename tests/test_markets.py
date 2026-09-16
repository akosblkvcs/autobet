"""The market-name vocabulary reduces names to bet types."""

import pytest

from autobet.markets import family

_SIDES = ("Montrose", "FC Dundee 2")


@pytest.mark.parametrize(
    "name",
    ["Montrose gólszám 3.5", "FC Dundee 2 gólszám 4.5"],
)
def test_family_collapses_line_and_team(name: str) -> None:
    """The same bet on either side, on any line, is one family."""
    assert family(name, _SIDES) == "{csapat} gólszám {N}"


def test_family_leaves_a_market_without_line_or_team_alone() -> None:
    """A name that mentions neither side nor a line is already its family."""
    assert family("Végeredmény", _SIDES) == "Végeredmény"


def test_family_replaces_the_longer_side_first() -> None:
    """One side's name can contain the other's, and the longer one must win."""
    assert family("FC Dundee 2 gólszám 4.5", ("Dundee", "FC Dundee 2")) == (
        "{csapat} gólszám {N}"
    )
