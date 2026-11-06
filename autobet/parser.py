"""Turn a message into a tip by reading its betslip screenshot."""

import base64
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import ContentBlockParam, TextBlockParam, Usage
from pydantic import BaseModel

from autobet import markets
from autobet.config import Settings
from autobet.models import IncomingMessage, Tip, TipLeg, combined, utcnow

log = structlog.get_logger(__name__)

_VISION_MODEL = "claude-opus-5-5"
_READING_SECONDS = 30.0

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

- sport: as the bet-type list names it, else in Hungarian.
- event: the two teams or competitors, as printed.
- market: the bookmaker's name for this bet, taken from the bet-type list
  when anything there is the same bet — wording and period included, since each
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

- sport: as the bet-type list names it, else in Hungarian.
- event: the two teams or competitors, as written.
- market: the bookmaker's name for this bet, taken from the bet-type list
  when anything there is the same bet — wording and period included, since each
  sport names its periods its own way. When nothing there is this bet, keep the
  tipster's own words rather than inventing a name. Keep any line they quoted,
  with its sign.
- selection: what is backed, labelled as the bookmaker would: a team name,
  "Igen"/"Nem", "Több, mint N"/"Kevesebb, mint N", "Döntetlen".
- odds: the decimal odds, or null when the tipster gives none. "1,82" and
  "1.82" are both 1.82.
"""


_READING_PROMPT = """A sports betting tipster's bet, in the tipster's words:

- sport: {sport}
- event: {event}
- market: {market}
- selection: {selection}

Comparing spellings could not place this bet. Below is the bookmaker's own list
for the {asked}: answer with the number of the entry that means the same thing,
or null when none does.

Spelling, language, abbreviation and how much of a name is written may all
differ. These may not:
- the competitors, with the first one named still the home side;
- which team of a club it is: women, reserves and age groups are other teams;
- the period and the line. A bet that names no period is on the whole match.

A wrong number loses money and null only skips the bet, so answer only what you
are sure of.

{options}
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


class _Reading(BaseModel):
    """Which numbered entry the model read a leg as, if any."""

    number: int | None


def build_claude(api_key: str) -> AsyncAnthropic:
    """Build a client for the Claude API, using the stored key."""
    return AsyncAnthropic(api_key=api_key)


def build_vocabulary(settings: Settings) -> str:
    """The bookmaker's own bet types, appended to whichever prompt runs."""
    return markets.as_prompt(markets.load(settings.market_families))


def _spend(usage: Usage) -> dict[str, int]:
    """How this call was billed, so the caching can be seen rather than assumed."""
    return {
        "cached": usage.cache_read_input_tokens or 0,
        "cache_written": usage.cache_creation_input_tokens or 0,
        "uncached": usage.input_tokens,
        "answered": usage.output_tokens,
    }


def _system(vocabulary: str) -> list[TextBlockParam]:
    """The bet-type list, cached because every tip sends the same bytes of it."""
    if not vocabulary:
        return []

    return [
        {
            "type": "text",
            "text": vocabulary,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]


def _content(message: IncomingMessage) -> list[ContentBlockParam] | None:
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
            {"type": "text", "text": _TEXT_PROMPT + "\n\nThe message:\n" + message.text}
        ]

    return None


async def read_leg(
    leg: TipLeg, asked: str, options: Sequence[str], *, claude: AsyncAnthropic
) -> int | None:
    """Which of the book's entries a leg means, by position, or None if none surely."""
    started = utcnow()
    listed = "\n".join(f"{number}. {option}" for number, option in enumerate(options, 1))
    bounded = claude.with_options(timeout=_READING_SECONDS, max_retries=0)
    response = await bounded.messages.parse(
        model=_VISION_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[
            {
                "role": "user",
                "content": _READING_PROMPT.format(
                    sport=leg.sport,
                    event=leg.event,
                    market=leg.market,
                    selection=leg.selection,
                    asked=asked,
                    options=listed,
                ),
            }
        ],
        output_format=_Reading,
    )
    reading = response.parsed_output
    number = 0 if reading is None else reading.number or 0
    chosen = number - 1 if 1 <= number <= len(options) else None

    log.info(
        "leg_read",
        fixture=leg.event,
        asked=asked,
        options=len(options),
        read=None if chosen is None else options[chosen],
        ms=int((utcnow() - started).total_seconds() * 1000),
        **_spend(response.usage),
    )

    return chosen


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
    content = _content(message)

    if content is None:
        return None
    started = utcnow()
    response = await claude.messages.parse(
        model=_VISION_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=_system(vocabulary),
        messages=[{"role": "user", "content": content}],
        output_format=_Slip,
    )
    slip = response.parsed_output
    vision_ms = int((utcnow() - started).total_seconds() * 1000)
    spend = _spend(response.usage)

    if slip is None or slip.kind != "to_place" or not slip.legs:
        log.info(
            "no_tip",
            external_id=message.external_id,
            kind=slip.kind if slip else None,
            vision_ms=vision_ms,
            **spend,
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
        **spend,
    )

    return Tip(
        legs=legs,
        odds=combined(legs),
        message=message,
    )
