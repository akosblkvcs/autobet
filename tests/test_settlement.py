"""Tests for bet settlement checking and outcome extraction."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autobet.bookmaker import _extract_settlement, _normalize_settlement
from autobet.models import IncomingMessage, MessageWithTip, TipLeg, Verdict


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WON", "won"),
        ("won", "won"),
        ("LOST", "lost"),
        ("lost", "lost"),
        ("HALF_WON", "half_won"),
        ("HALF_LOST", "half_lost"),
        ("VOID", "void"),
        ("CANCELLED", "void"),
        ("CANCELED", "void"),
        ("REJECTED", "void"),
        ("CASHED_OUT", "cashed_out"),
        ("OPEN", "pending"),
        ("PENDING", "pending"),
        ("UNKNOWN_STATUS", None),
    ],
)
def test_normalize_settlement(raw: str, expected: str | None) -> None:
    """Normalize bookmaker bet status to domain settlement enum."""
    assert _normalize_settlement(raw) == expected


def test_extract_settlement_won_with_payout() -> None:
    """Extract won status and payout from bookmaker details."""
    details = {"betId": "12345", "status": "WON", "payout": 350.50}
    outcome, returned = _extract_settlement(details)
    assert outcome == "won"
    assert returned == Decimal("350.50")


def test_extract_settlement_lost() -> None:
    """Extract lost status with no payout."""
    details = {"betId": "12345", "betStatus": "LOST"}
    outcome, returned = _extract_settlement(details)
    assert outcome == "lost"
    assert returned is None


def test_message_with_tip_settlement_property() -> None:
    """MessageWithTip aggregates verdicts into an overall tip outcome."""
    now = datetime.now(timezone.utc)
    msg = IncomingMessage(
        external_id="100:1",
        channel="Test",
        sent_at=now,
        received_at=now,
        text="tip",
    )
    legs = (
        TipLeg(
            sport="Labdarúgás",
            event="A - B",
            market="1X2",
            selection="A",
            odds=2.0,
        ),
    )

    # Won verdict
    tip_won = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(Verdict(who="user1", reference="b1", settlement="won"),),
    )
    assert tip_won.settlement == "won"

    # Lost verdict
    tip_lost = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(Verdict(who="user1", reference="b2", settlement="lost"),),
    )
    assert tip_lost.settlement == "lost"

    # Pending verdict
    tip_pending = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(Verdict(who="user1", reference="b3", settlement="pending"),),
    )
    assert tip_pending.settlement == "pending"

    # Mixed verdicts: if at least one won, outcome is won
    tip_mixed = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(
            Verdict(who="user1", reference="b4", settlement="won"),
            Verdict(who="user2", reference="b5", settlement="lost"),
        ),
    )
    assert tip_mixed.settlement == "won"

    # Half won verdict
    tip_half_won = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(Verdict(who="user1", reference="b6", settlement="half_won"),),
    )
    assert tip_half_won.settlement == "half_won"

    # Half lost verdict
    tip_half_lost = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(Verdict(who="user1", reference="b7", settlement="half_lost"),),
    )
    assert tip_half_lost.settlement == "half_lost"

    # Cashed out verdict
    tip_cashed_out = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(Verdict(who="user1", reference="b8", settlement="cashed_out"),),
    )
    assert tip_cashed_out.settlement == "cashed_out"

    # Void/cancelled verdict
    tip_void = MessageWithTip(
        message=msg,
        legs=legs,
        verdicts=(Verdict(who="user1", reference="b9", settlement="void"),),
    )
    assert tip_void.settlement == "void"
