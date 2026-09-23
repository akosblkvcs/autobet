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

_SLIP_PROMPT = """This image was posted by a sports betting tipster. Decide
what it is, then read it.

kind:
- "to_place" — a slip proposing a bet that has not been settled: selections
  with their odds, and no result.
- "settled" — anything reporting what already happened, however many numbers it
  holds: a won or lost slip, a results announcement, a month's recap with ticks,
  crosses, a profit total or a balance. **A recap of many settled bets is never
  an accumulator**: those bets are separate and already finished, and staking
  them as one slip would be a wager nobody proposed.
- "other" — anything else: marketing, a league header, a photo that is no slip.

Only "to_place" is a bet we can make; return one leg per selection, in the
order shown.

- sport: as the bet-type list below names it, else in Hungarian.
- event: the two teams or competitors, as printed.
- market: the bookmaker's name for this bet, taken from the list below when
  anything there is the same bet — wording and period included, since each
  sport names its periods its own way. When nothing there is this bet, keep the
  slip's own words rather than inventing a name. Keep any line it prints, with
  its sign.
- selection: the outcome backed, as printed.
- odds: the decimal odds, or null when the slip does not price the leg. A slip
  writes "1,82" and "1.82" for the same 1.82.
"""


_TEXT_PROMPT = """This message was posted by a sports betting tipster. Decide
what it is, then read it.

kind:
- "to_place" — it proposes a bet that has not been settled yet.
- "settled" — it reports what already happened, however many numbers it holds:
  a won or lost tip, a results announcement, a month's summary.
- "other" — anything else: advertising, commentary, a greeting.

Only "to_place" is a bet we can make; return one leg per selection.

- sport: as the bet-type list below names it, else in Hungarian.
- event: the two teams or competitors, as written.
- market: the bookmaker's name for this bet, taken from the list below when
  anything there is the same bet — wording and period included, since each
  sport names its periods its own way. When nothing there is this bet, keep the
  tipster's own words rather than inventing a name. Keep any line they quoted,
  with its sign.
- selection: what is backed, labelled as the bookmaker would: a team name,
  "Igen"/"Nem", "Több, mint N"/"Kevesebb, mint N", "Döntetlen".
- odds: the decimal odds, or null when the tipster gives none. "1,82" and
  "1.82" are both 1.82.
"""


class _Leg(BaseModel):
    """One selection, as Claude reads it off the image."""

    sport: str
    event: str
    market: str
    selection: str
    odds: float | None = None
    """What the tipster quoted, and None when they quoted nothing."""


class _Slip(BaseModel):
    """What the image is, and the selections on it if it proposes a bet."""

    kind: Literal["to_place", "settled", "other"]
    legs: list[_Leg]


def build_claude(api_key: str) -> AsyncAnthropic:
    """Build a client for the Claude API, using the stored key."""
    return AsyncAnthropic(api_key=api_key)


def build_vocabulary(settings: Settings) -> str:
    """The bookmaker's own bet types, appended to whichever prompt runs."""
    return markets.as_prompt(markets.load(settings.market_families))


def _content(message: IncomingMessage, vocabulary: str) -> list[ContentBlockParam] | None:
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
            {"type": "text", "text": _SLIP_PROMPT + vocabulary},
        ]

    if message.text.strip():
        return [
            {
                "type": "text",
                "text": _TEXT_PROMPT + vocabulary + "\n\nThe message:\n" + message.text,
            }
        ]

    return None


async def parse_tip(
    message: IncomingMessage,
    *,
    claude: AsyncAnthropic,
    vocabulary: str,
) -> Tip | None:
    """Extract a tip from a message's screenshot, or None if there is not one.

    Args:
        message: The archived message: its screenshot if it has one, else its text.
        claude: Client used for the extraction.
        vocabulary: The book's bet types, from :func:`build_vocabulary`.

    Returns:
        The tip the message describes, or None for anything that is not one.
    """
    content = _content(message, vocabulary)

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
            sport=leg.sport,
            event=leg.event,
            market=leg.market,
            selection=leg.selection,
            odds=leg.odds if leg.odds else None,
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
        message=message,
    )
