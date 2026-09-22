"""What the `settings` table holds, and what each person overrides of it."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Policy(BaseModel):
    """Every limit an admin can change without a deploy, and the default in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paper: bool = Field(
        default=True,
        description="Force every account to paper, whatever each person chose.",
    )
    stake: Decimal = Field(
        default=Decimal("100"),
        ge=100,
        max_digits=12,
        decimal_places=2,
        description="Flat amount staked on every tip, until a user sets their own.",
    )
    max_odds_drop_percent: float = Field(
        default=10.0,
        gt=0,
        description="Abandon the bet when the live price fell this far below the tip.",
    )
    mismatch_rise_percent: int = Field(
        default=50,
        gt=0,
        description="A price this far above the tip is a different bet, not a bargain.",
    )


class Mode(StrEnum):
    """Whether somebody's bets reach the book at all."""

    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class Terms:
    """What one person's bet is judged against: no blanks left to resolve."""

    mode: Mode
    paused: bool
    stake: Decimal
    max_odds_drop_percent: float
    mismatch_rise_percent: int


class UserPolicy(BaseModel):
    """One person's own terms. A field left None inherits the service default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Mode = Field(
        default=Mode.PAPER,
        description="paper resolves, prices and records without sending anything.",
    )
    paused: bool = Field(
        default=False,
        description="Stake nothing for this person until they say otherwise.",
    )
    stake: Decimal | None = Field(
        default=None,
        ge=100,
        max_digits=12,
        decimal_places=2,
        description="What this person stakes per tip; blank follows the service.",
    )
    max_odds_drop_percent: float | None = Field(
        default=None,
        gt=0,
        description="This person's drop limit; blank follows the service.",
    )

    def over(self, policy: Policy) -> Terms:
        """These terms with the service's defaults filled in where none was set."""
        return Terms(
            mode=Mode.PAPER if policy.paper else self.mode,
            paused=self.paused,
            stake=policy.stake if self.stake is None else self.stake,
            max_odds_drop_percent=(
                policy.max_odds_drop_percent
                if self.max_odds_drop_percent is None
                else self.max_odds_drop_percent
            ),
            mismatch_rise_percent=policy.mismatch_rise_percent,
        )


SECRETS = frozenset({"telegram_api_hash", "claude_api_key"})


class Integrations(BaseModel):
    """The keys the service itself holds, read at startup and admin-editable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    telegram_api_id: int = Field(
        default=0,
        ge=0,
        description="Telegram app id from my.telegram.org.",
    )
    telegram_api_hash: str = Field(
        default="",
        description="Telegram app hash from my.telegram.org.",
    )
    claude_api_key: str = Field(
        default="",
        description="Claude API key, a plain one rather than identity-linked.",
    )
