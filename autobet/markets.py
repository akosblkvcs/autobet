"""The bet types this bookmaker offers, in its own words.

The parser has to name a market the way the site names it, and guessing that
from memory is what put `Végeredmény (kétesély nélkül)` -- a market that does
not exist -- on a basketball tip. So the vocabulary is harvested from the feed
rather than written down: :func:`harvest` walks a few events per sport and
keeps each market name with its line and its teams replaced, which collapses
the ~90 names one match lists into ~25 bet types.
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import structlog

from autobet.feed import Feed, IndexedEvent, two_sides

log = structlog.get_logger(__name__)

_LINE = re.compile(r"-?\d+[.,]?\d*")
_PER_SPORT = 3


def family(name: str, sides: tuple[str, str] | None) -> str:
    """One market name reduced to the bet it is, without its line or its teams.

    `Montrose gólszám 3.5` and `FC Dundee 2 gólszám 4.5` are the same bet type,
    so both become `{csapat} gólszám {N}`.
    """
    for side in sorted(sides or (), key=len, reverse=True):
        name = name.replace(side, "{csapat}")

    return " ".join("{N}" if _LINE.fullmatch(word) else word for word in name.split())


def families_of(records: list[dict[str, Any]], event: IndexedEvent) -> set[str]:
    """Every bet type one event's market list holds."""
    sides = two_sides(event.name)

    return {
        family(str(record["name"]), sides)
        for record in records
        if record["_type"] == "MARKET"
    }


async def harvest(feed: Feed, events: list[IndexedEvent]) -> dict[str, list[str]]:
    """Collect the bet types on offer, a few events per sport.

    A market can name a single player rather than a team -- baseball lists
    `A.J. Ewing RBI-ok száma {N}` -- and no template collapses those. They are
    dropped by keeping only families that more than one sampled event lists,
    since a player prop belongs to one match and a bet type belongs to all of
    them. That took baseball from 551 families to a usable list.
    """
    sampled: dict[str, list[IndexedEvent]] = defaultdict(list)
    for event in events:
        if len(sampled[event.sport]) < _PER_SPORT:
            sampled[event.sport].append(event)

    vocabulary: dict[str, list[str]] = {}
    for sport, chosen in sorted(sampled.items()):
        seen: Counter[str] = Counter()
        for event in chosen:
            seen.update(families_of(await feed.market_records(event), event))

        shared = 1 if len(chosen) == 1 else 2
        names = sorted(name for name, count in seen.items() if count >= shared)

        if names:
            vocabulary[sport] = names

        log.info("markets_harvested", sport=sport, kept=len(names), saw=len(seen))

    return vocabulary


def load(path: Path) -> dict[str, list[str]]:
    """The harvested vocabulary, or nothing if it has never been harvested."""
    if not path.exists():
        log.warning("markets_unharvested", path=str(path))

        return {}

    vocabulary: dict[str, list[str]] = json.loads(path.read_text())

    return vocabulary


def save(vocabulary: dict[str, list[str]], path: Path) -> None:
    """Write the vocabulary where :func:`load` will find it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(vocabulary, ensure_ascii=False, indent=1))


def as_prompt(vocabulary: dict[str, list[str]]) -> str:
    """The vocabulary as the parser shows it to the model, or "" if empty."""
    if not vocabulary:
        return ""

    listed = "\n".join(
        f"{sport}:\n" + "\n".join(f"  {name}" for name in names)
        for sport, names in vocabulary.items()
    )

    return f"""
These are the bet types this bookmaker lists, by sport. Name the market with
the one that fits, keeping its wording exactly, with `{{N}}` replaced by the
line and `{{csapat}}` by the team. Return no legs if none of them is the bet.

{listed}
"""
