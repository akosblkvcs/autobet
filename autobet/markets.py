"""The bet types this bookmaker offers, in its own words."""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import structlog

from autobet.connection import Connection
from autobet.matching import IndexedEvent, two_sides

log = structlog.get_logger(__name__)

_LINE = re.compile(r"-?\d+[.,]?\d*")
_PER_SPORT = 8


def family(name: str, sides: tuple[str, str] | None) -> str:
    """One market name reduced to the bet it is, without its line or its teams."""
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


async def harvest(
    connection: Connection, events: list[IndexedEvent]
) -> dict[str, list[str]]:
    """Collect the bet types on offer, from a sport's eight biggest fixtures."""
    sampled: dict[str, list[IndexedEvent]] = defaultdict(list)
    for event in sorted(events, key=lambda one: -one.markets):
        if len(sampled[event.sport]) < _PER_SPORT:
            sampled[event.sport].append(event)

    vocabulary: dict[str, list[str]] = {}
    for sport, chosen in sorted(sampled.items()):
        seen: Counter[str] = Counter()
        for event in chosen:
            seen.update(families_of(await connection.match_odds(event.id), event))

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


def merge(old: dict[str, list[str]], new: dict[str, list[str]]) -> dict[str, list[str]]:
    """Every bet type either run found, because one run is a lossy sample."""
    return {
        sport: sorted(set(old.get(sport, ())) | set(new.get(sport, ())))
        for sport in sorted(set(old) | set(new))
    }


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
