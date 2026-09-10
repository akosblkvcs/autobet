"""Turn a message into a tip by reading its betslip screenshot."""

import base64
from pathlib import Path
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import ContentBlockParam
from pydantic import BaseModel

from autobet import markets
from autobet.config import Settings
from autobet.models import IncomingMessage, Tip, TipLeg, combined, utcnow

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


_TEXT_PROMPT = """This message was posted by a sports betting tipster. Decide
what it is, then read it.

kind:
- "to_place" — it proposes a bet that has not been settled yet: an event, what
  is being backed, and the odds.
- "settled" — it reports what already happened: a won or lost tip, a results
  announcement, or a summary of a month's tips with hit rates and profit.
- "other" — anything else: advertising a group, commentary, a greeting.

Only "to_place" is a bet we can make. **A summary of past tips is "settled",
never a bet**, however many numbers it contains.

For "to_place", return one leg per selection. Most messages hold one; several
mean an accumulator.

- event: the two teams or competitors, exactly as written in the message.
- market: **the bookmaker's name for the bet type, not the tipster's phrasing.**
  The tipster writes prose; translate it to the market the bookmaker lists, in
  Hungarian, including the event part. For example:
    "X nyer (rendes játékidő)"         -> "1X2 - Rendes játékidő"
    "Over 2.5 gól"                     -> "Gólszám 2.5 - Rendes játékidő"
    "X -2 ázsiai hendikep"             -> "Ázsiai hendikep -2 - Rendes játékidő"
  A handicap or total line belongs in the market name and never in the
  selection: the market is "Ázsiai hendikep -2" and the selection is the plain
  team name. If you cannot map it confidently to a market a bookmaker would
  list, return no legs rather than inventing one.
- selection: what is being backed, as the bookmaker would label it — a team
  name for a winner market, "Igen"/"Nem" for both-teams-to-score, "Több, mint
  N"/"Kevesebb, mint N" for totals, "Döntetlen" for a draw.
- odds: the decimal odds, as a number.
"""


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
    """Build a client for the Claude API, using the configured key."""
    return AsyncAnthropic(api_key=settings.claude_api_key)


def build_text_prompt(settings: Settings) -> str:
    """The text prompt, with the bookmaker's own bet types appended to it."""
    return _TEXT_PROMPT + markets.as_prompt(markets.load(settings.market_families))


def _content(
    message: IncomingMessage, text_prompt: str
) -> list[ContentBlockParam] | None:
    """What to send the model for this message, or None if there is nothing."""
    if message.media_path is not None:
        image = base64.standard_b64encode(Path(message.media_path).read_bytes()).decode()

        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image,
                },
            },
            {"type": "text", "text": _SLIP_PROMPT},
        ]

    if message.text.strip():
        return [
            {"type": "text", "text": text_prompt + "\n\nThe message:\n" + message.text}
        ]

    return None


async def parse_tip(
    message: IncomingMessage,
    claude: AsyncAnthropic,
    stake: float,
    text_prompt: str,
) -> Tip | None:
    """Extract a tip from a message's screenshot, or None if there is not one.

    Args:
        message: The archived message: its screenshot if it has one, else its text.
        claude: Client used for the extraction.
        stake: What to stake, since the tip does not decide that.
        text_prompt: The prompt for a text tip, from :func:`build_text_prompt`.

    Returns:
        The tip the message describes, or None for anything that is not one.
    """
    content = _content(message, text_prompt)

    if content is None:
        return None
    started = utcnow()
    response = await claude.messages.parse(
        model=_VISION_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": content}],
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
        TipLeg(
            event=leg.event,
            market=leg.market,
            selection=leg.selection,
            odds=leg.odds if leg.odds > 0 else None,
        )
        for leg in slip.legs
    )

    log.info(
        "tip_extracted",
        external_id=message.external_id,
        legs=len(legs),
        priced=sum(leg.odds is not None for leg in legs),
        vision_ms=vision_ms,
    )

    return Tip(
        legs=legs,
        odds=combined(legs),
        stake=stake,
        message=message,
    )
