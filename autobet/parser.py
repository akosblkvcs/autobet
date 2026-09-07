"""Turn a message into a tip by reading its betslip screenshot."""

import base64
import math
from pathlib import Path
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from autobet.config import Settings
from autobet.models import IncomingMessage, Tip, TipLeg, utcnow

log = structlog.get_logger(__name__)

_VISION_MODEL = "claude-opus-5"
_SLIP_PROMPT = """This image was posted by a sports betting tipster. Decide what
it is, then read it.

kind:
- "to_place" — a betting slip proposing a bet that has not been settled: it
  shows selections with their odds, and no result.
- "settled" — anything reporting what already happened: a won or lost slip, a
  results announcement, or a month-end recap listing many past bets, usually
  with ticks, crosses, a profit total or a balance.
- "other" — anything else, such as marketing, a league header or a photo that
  is not a slip at all.

Only "to_place" is a bet we can make. **A recap listing many settled bets is
"settled", never an accumulator** — the bets on it are separate and already
finished, and staking them as one slip would be a wager nobody proposed.

For "to_place", return one leg per selection, in the order shown; several
selections mean an accumulator. For anything else return no legs.

- event: the two teams or competitors, as printed.
- market: the bet type, as printed, such as "1X2 - Rendes játékidő" or
  "Money Line - Match".
- selection: the outcome being backed, as printed.
- odds: the decimal odds for that leg. The slips use both a comma and a point
  as the decimal separator, so "1,82" and "1.82" are both 1.82.

Keep the wording exactly as it appears, in its original language; it has to
match the bookmaker's own page later."""


class _Leg(BaseModel):
    """One selection, as Claude reads it off the image."""

    event: str
    market: str
    selection: str
    odds: float


class _Slip(BaseModel):
    """What the image is, and the selections on it if it proposes a bet."""

    kind: Literal["to_place", "settled", "other"]
    legs: list[_Leg]


def build_claude(settings: Settings) -> AsyncAnthropic:
    """Build the vision client, mirroring ``build_client`` for Telegram."""
    return AsyncAnthropic(api_key=settings.claude_api_key)


async def parse_tip(
    message: IncomingMessage, claude: AsyncAnthropic, stake: float
) -> Tip | None:
    """Extract a tip from a message's screenshot, or None if there is not one.

    Args:
        message: The archived message, whose ``media_path`` is read if set.
        claude: Client used for the vision call.
        stake: What to stake, since the screenshot does not decide that.

    Returns:
        The tip the screenshot describes, or None for anything without one.
    """
    if message.media_path is None:
        return None

    image = base64.standard_b64encode(Path(message.media_path).read_bytes()).decode()
    started = utcnow()
    response = await claude.messages.parse(
        model=_VISION_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image,
                        },
                    },
                    {"type": "text", "text": _SLIP_PROMPT},
                ],
            }
        ],
        output_format=_Slip,
    )
    slip = response.parsed_output
    vision_ms = int((utcnow() - started).total_seconds() * 1000)

    if slip is None or slip.kind != "to_place" or not slip.legs:
        log.info(
            "no_tip",
            external_id=message.external_id,
            kind=slip.kind if slip else None,
            vision_ms=vision_ms,
        )

        return None

    legs = tuple(
        TipLeg(event=leg.event, market=leg.market, selection=leg.selection, odds=leg.odds)
        for leg in slip.legs
    )

    log.info(
        "tip_extracted",
        external_id=message.external_id,
        legs=len(legs),
        vision_ms=vision_ms,
    )

    return Tip(
        legs=legs,
        odds=math.prod(leg.odds for leg in legs),
        stake=stake,
        message=message,
    )
