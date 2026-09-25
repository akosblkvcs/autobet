"""Turning a tipster's wording into the bookmaker's own event, market and outcome.

Fuzz the wording, never the structure: team names and market phrasing vary, while
handicap lines, halves and the number of combined legs are the bet itself.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from rapidfuzz import fuzz

from autobet.models import LegOffer, LegResolution, SelectionStatus, TipLeg, utcnow

log = structlog.get_logger(__name__)

_MIN_SCORE = 0.85
_MIN_GAP = 0.05

_FIXTURE_SIDES = re.compile(r" - | vs\.? ")
_DECIMAL_LINE = re.compile(r"(\d+)[.,](\d+)")
_SCORE = re.compile(r"(\d+)\s*:\s*(\d+)")
_SELECTION_PARTS = re.compile(r"\s*(?:/|,|\bvagy\b|\bor\b)\s*")
_SHORTHAND = re.compile(r"[1x2]+")
_TEAM_SLOT = "{csapat}"
_LINE_SLOT = "{N}"
_SIDED_LINE = re.compile(r"\(([-+]?\d+(?:[.,]\d+)?)\)")
_LINE_TOKEN = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_NUMERIC = re.compile(r"[-+]?\d+\.?\d*")
_TRANSLITERATED = str.maketrans({"j": "i", "y": "i", "w": "v", "k": "c"})
_AGE = re.compile(r"\bu(\d{2})")


def fold(text: str) -> str:
    """Strip accents, case and transliteration differences."""
    stripped = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in stripped if not unicodedata.combining(c))

    return plain.casefold().translate(_TRANSLITERATED).strip()


def _folded(*words: str) -> tuple[str, ...]:
    """Fold a vocabulary at import, so lookups compare like with like."""
    return tuple(fold(word) for word in words)


_SELECTION_KEYS = dict(
    zip(
        _folded("Igen", "Nem", "Yes", "No", "Döntetlen", "Draw", "X"),
        ("yes", "no", "yes", "no", "draw", "draw", "draw"),
        strict=True,
    )
)
_MARKS = {
    "reserve": frozenset(_folded("B", "II", "III", "2", "3", "reserves", "reserve")),
    "women": frozenset(_folded("W", "women", "női", "noi")),
}
_DRAW_WORDS = _folded("Döntetlen", "Draw", "X")
_OVER_WORDS = _folded("Több", "Over")
_UNDER_WORDS = _folded("Kevesebb", "Under")
_SIDE_WORDS = {"1": "home", "x": "draw", "2": "away"}
_SIDE_CODES = {"home": "#HOME", "away": "#AWAY", "draw": "#D"}
_SIDE_KEYS = {
    frozenset({"home"}): "home",
    frozenset({"away"}): "away",
    frozenset({"draw"}): "draw",
    frozenset({"home", "draw"}): "home_draw",
    frozenset({"away", "draw"}): "away_draw",
    frozenset({"home", "away"}): "home_away",
}


def _normalise(name: str) -> str:
    """Reduce a market name to what the slip and the feed agree on."""
    name = _SCORE.sub(lambda m: f"{m.group(1)}-{m.group(2)}", name)
    name = name.replace("–", "-").replace("—", "-")

    return " ".join(
        _DECIMAL_LINE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", name).split()
    ).casefold()


def _shape(name: str) -> tuple[int, frozenset[float]]:
    """What a market bets on: how many things at once, and at which lines."""
    words = _normalise(name).split()

    lines = frozenset(abs(float(word)) for word in words if _NUMERIC.fullmatch(word))

    return sum(word == "+" for word in words), lines


def _lined(market: str, leg: TipLeg) -> list[str]:
    """The market with a `{N}` the model left behind filled from the selection."""
    if _LINE_SLOT not in market:
        return [market]

    return [market.replace(_LINE_SLOT, f"{line:g}") for line in _lines_named(leg)]


def _sided(market: str, leg: TipLeg, event: IndexedEvent) -> list[str]:
    """The market with the team named as the book names it, as well as as written."""
    sides = two_sides(event.name)

    if sides is None:
        return [market]

    if _TEAM_SLOT in market:
        return [market.replace(_TEAM_SLOT, side) for side in sides]

    words = market.split()

    for length in (1, 2, 3):
        named = event.side_of(" ".join(words[:length]))

        if named is not None:
            side = sides[0] if named == "home" else sides[1]

            return [market, " ".join([side, *words[length:]])]

    return [market]


def _asked(leg: TipLeg, event: IndexedEvent) -> list[str]:
    """Every market name this leg could mean, with the slots the model left filled."""
    return [
        asked
        for market in _lined(leg.market, leg)
        for asked in _sided(market, leg, event)
    ]


def _market_score(wanted: str, candidate: str) -> float:
    """How well a tipster's market name matches one the feed lists."""
    if _shape(wanted) != _shape(candidate):
        return 0.0

    return fuzz.token_sort_ratio(_normalise(wanted), _normalise(candidate)) / 100


def _top[Candidate](scored: list[tuple[float, Candidate]]) -> list[Candidate]:
    """Every candidate the score cannot separate from the best, or none at all."""
    if not scored:
        return []

    ranked = sorted(scored, key=lambda pair: pair[0], reverse=True)
    best = ranked[0][0]

    if best < _MIN_SCORE:
        return []

    return [candidate for score, candidate in ranked if best - score < _MIN_GAP]


def _best[Candidate](scored: list[tuple[float, Candidate]]) -> Candidate | None:
    """The one clear winner among scored candidates, or None if there is not one."""
    top = _top(scored)

    return top[0] if len(top) == 1 else None


def _initials(folded: str) -> str:
    """Abbreviation hit helper, so `ANM` reaches `Atl. Nacional Medellin`."""
    return "".join(word[0] for word in folded.split() if word)


def two_sides(name: str) -> tuple[str, str] | None:
    """Split `A - B` or `A vs. B` into its two competitors, or None."""
    parts = [part.strip() for part in _FIXTURE_SIDES.split(name)]

    return (parts[0], parts[1]) if len(parts) == 2 else None  # noqa: PLR2004


def _marks(name: str) -> frozenset[str]:
    """Which team of a club a name points at: the reserves, the women, an age group."""
    folded = fold(name)
    words = {word.strip("().,") for word in folded.split()}
    marks = {mark for mark, words_of in _MARKS.items() if words & words_of}

    return frozenset(marks | {f"u{age}" for age in _AGE.findall(folded)})


def _score(query: str, aliases: Sequence[str]) -> float:
    """How well one competitor matches any name the feed has for that side."""
    folded = fold(query)
    marks = _marks(query)

    return max(
        (
            0.0
            if marks - _marks(alias)
            else 0.95
            if folded == _initials(alias)
            else fuzz.WRatio(folded, alias) / 100
            for alias in aliases
        ),
        default=0.0,
    )


def _starts_at(event: dict[str, Any]) -> datetime | None:
    """When the feed says this event starts; its `startTime` is epoch millis."""
    when = event.get("startTime")

    return datetime.fromtimestamp(when / 1000, UTC) if when else None


@dataclass(frozen=True, slots=True)
class IndexedEvent:
    """One fixture in the index, with every name the feed gives its two sides."""

    id: str
    tournament_id: str
    name: str
    sport: str
    home_id: str
    away_id: str
    home: tuple[str, ...]
    away: tuple[str, ...]
    starts_at: datetime | None
    markets: int = 0
    """How many markets the fixture declares, which is what `harvest` samples by."""

    @property
    def started(self) -> bool:
        """Whether kick-off has passed, which is when this stops being bettable."""
        return self.starts_at is not None and self.starts_at < utcnow()

    def side_of(self, competitor: str) -> str | None:
        """Which side a name picks out, or None when it fits both or neither."""
        home, away = _score(competitor, self.home), _score(competitor, self.away)

        if max(home, away) < _MIN_SCORE or abs(home - away) < _MIN_GAP:
            return None

        return "home" if home > away else "away"


@dataclass(frozen=True, slots=True)
class IndexedTournament:
    """One competition as the walk found it, and the two things a re-walk needs."""

    id: str
    sport_id: str
    upcoming: int


def _aliases(records: Sequence[dict[str, Any]], side: str) -> tuple[str, ...]:
    """Every name the feed gave one side of a fixture, folded and deduplicated."""
    names = {
        fold(str(record.get(key) or ""))
        for record in records
        for key in (f"{side}ParticipantName", f"{side}ShortParticipantName")
    }

    return tuple(sorted(name for name in names if name))


def indexed_event(records: Sequence[dict[str, Any]], tournament_id: str) -> IndexedEvent:
    """Fold one fixture's per-language records into a single index entry."""
    first = records[0]

    return IndexedEvent(
        id=str(first["id"]),
        tournament_id=tournament_id,
        name=str(first.get("name") or ""),
        sport=str(first.get("sportName") or ""),
        home_id=str(first.get("homeParticipantId") or ""),
        away_id=str(first.get("awayParticipantId") or ""),
        home=_aliases(records, "home"),
        away=_aliases(records, "away"),
        starts_at=_starts_at(first),
        markets=int(first.get("numberOfMarkets") or 0),
    )


def find_event(
    events: Sequence[IndexedEvent], fixture: str, sport: str = ""
) -> IndexedEvent | None:
    """Which indexed event a tip's event line names, or None if unclear."""
    sides = two_sides(fixture)
    if sides is None:
        return None

    candidates = [event for event in events if event.sport == sport] or events
    found = _best(
        [
            ((_score(sides[0], event.home) + _score(sides[1], event.away)) / 2, event)
            for event in candidates
        ]
    )

    if found is None:
        log.info("event_unclear", fixture=fixture, sport=sport, indexed=len(candidates))

    return found


def _parts(selection: str) -> list[str]:
    """The outcomes a selection names, one per part."""
    folded = fold(selection)

    if _SHORTHAND.fullmatch(folded):
        return list(folded)

    return [part for part in _SELECTION_PARTS.split(selection) if part.strip()]


def _piece(text: str, event: IndexedEvent) -> str | None:
    """Which side of the fixture one part of a selection names."""
    folded = fold(text)

    if folded in _DRAW_WORDS:
        return "draw"

    return _SIDE_WORDS.get(folded) or event.side_of(text)


def _over_under(folded: str) -> str | None:
    """Whether a totals selection backs the over or the under."""
    if folded.startswith(_OVER_WORDS):
        return "over"
    if folded.startswith(_UNDER_WORDS):
        return "under"

    return None


def _line(text: str) -> float:
    """One signed line, however it was punctuated."""
    return float(text.replace(",", "."))


def _lines_named(leg: TipLeg) -> frozenset[float]:
    """Every signed line the leg names, in its market or in its selection."""
    spelled = (
        _normalise(f"{leg.market} {leg.selection}").replace("(", " ").replace(")", " ")
    )

    return frozenset(
        _line(word) for word in spelled.split() if _LINE_TOKEN.fullmatch(word)
    )


def _backs(
    outcome: dict[str, Any],
    leg: TipLeg,
    wanted: tuple[str | None, str | None],
    sides: dict[str, str],
) -> int | None:
    """How specifically an outcome is the bet a leg names; lower is better."""
    key, code = wanted

    sided = _SIDED_LINE.search(str(outcome.get("translatedName") or ""))

    if sided is not None and _line(sided.group(1)) not in _lines_named(leg):
        return None

    shown = fold(_normalise(str(outcome.get("translatedName") or "")))

    if shown and shown == fold(_normalise(leg.selection)):
        return 0

    marked = outcome.get("code") or ""
    for participant, side in sides.items():
        if participant:
            marked = marked.replace(f"#P{participant}", side)

    if code and marked == code:
        return 1

    if key is not None and outcome.get("headerNameKey") == key:
        return 2

    return None


def _bare(selection: str, event: IndexedEvent) -> str:
    """The selection with a leading team dropped, which is how the book prints one."""
    words = selection.replace(":", " ").split()

    for length in (1, 2, 3):
        if len(words) > length and event.side_of(" ".join(words[:length])):
            return " ".join(words[length:])

    return selection


def _selection(leg: TipLeg, event: IndexedEvent) -> tuple[str | None, str | None]:
    """What the leg backs, as a header key and as an outcome code."""
    folded = fold(leg.selection)
    pieces = [_piece(part, event) for part in _parts(leg.selection)]
    named = [piece for piece in pieces if piece is not None]
    whole: frozenset[str] = frozenset(named) if len(named) == len(pieces) else frozenset()

    key = (
        _SELECTION_KEYS.get(folded)
        or _over_under(folded)
        or _over_under(fold(_bare(leg.selection, event)))
        or _SIDE_KEYS.get(whole)
    )
    code = " / ".join(_SIDE_CODES[piece] for piece in named) if whole else None

    return key, code


def pick_offer(
    leg: TipLeg, event: IndexedEvent, records: Sequence[dict[str, Any]]
) -> LegResolution:
    """The one offer a leg names among an event's markets, or how far it got."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["_type"], []).append(record)

    asked = _asked(leg, event)
    named = {
        market["id"]: market
        for market in _top(
            [
                (max(_market_score(a, str(m["name"])) for a in asked), m)
                for m in grouped.get("MARKET", [])
            ]
        )
    }
    if not named:
        log.info("market_unmatched", fixture=event.name, market=leg.market)

        return LegResolution(SelectionStatus.NO_MARKET)

    market_of = {
        relation["outcomeId"]: relation["marketId"]
        for relation in grouped.get("MARKET_OUTCOME_RELATION", [])
    }
    prices = {offer["outcomeId"]: offer for offer in grouped.get("BETTING_OFFER", [])}
    wanted = _selection(leg, event)
    sides = {event.home_id: "#HOME", event.away_id: "#AWAY"}
    matched = [
        (tier, named[market_of[outcome["id"]]], outcome)
        for outcome in grouped.get("OUTCOME", [])
        if market_of.get(outcome["id"]) in named
        and outcome["id"] in prices
        and (tier := _backs(outcome, leg, wanted, sides)) is not None
    ]
    closest = min((tier for tier, _, _ in matched), default=0)
    found = [(market, outcome) for tier, market, outcome in matched if tier == closest]

    if len(found) == 1:
        market, outcome = found[0]
        offer = prices[outcome["id"]]

        return LegResolution(
            SelectionStatus.RESOLVED,
            LegOffer(
                leg=leg,
                event_id=event.id,
                event_name=event.name,
                market_id=str(market["id"]),
                outcome_id=str(outcome["id"]),
                betting_type_id=str(market.get("bettingTypeId")),
                offer_id=str(offer["id"]),
                odds=float(offer["odds"]),
                starts_at=event.starts_at,
            ),
        )

    log.info(
        "outcome_unmatched",
        fixture=event.name,
        selection=leg.selection,
        matched=len(found),
    )

    # Several outcomes at the same tier means the wording fits more than one bet.
    if len(found) > 1:
        return LegResolution(SelectionStatus.AMBIGUOUS)

    return LegResolution(SelectionStatus.NO_OUTCOME)
