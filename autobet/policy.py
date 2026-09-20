"""What the `settings` table holds: the betting limits and the service's keys."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Policy(BaseModel):
    """Every limit an admin can change without a deploy, and the default in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stake: Decimal = Field(
        default=Decimal("100"),
        ge=100,
        description="Flat amount staked on every tip, until a user sets their own.",
    )
    max_odds_drop_percent: float = Field(
        default=10.0,
        gt=0,
        description="Abandon the bet when the live price fell this far below the tip.",
    )
    mismatch_rise_percent: float = Field(
        default=44.0,
        gt=0,
        description="A price this far above the tip is a different bet, not a bargain.",
    )


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
