"""Resolve a tip against the live feed, then stake it."""

import math
from dataclasses import replace

import structlog

from autobet.config import Settings
from autobet.feed import Feed
from autobet.models import BetResult, LegOffer, Tip, utcnow
from autobet.session import mint_ce_session

log = structlog.get_logger(__name__)


class Bookmaker:
    """The bookmaker representation. Stakes only when dry run is off."""

    def __init__(self, settings: Settings) -> None:
        """Store the settings and build the feed client."""
        self._settings = settings
        self._dry_run = settings.dry_run
        self._max_drop_percent = settings.max_odds_drop_percent
        self._max_event_days_ahead = settings.max_event_days_ahead
        self._forced = set(settings.telegram_force_chat_ids)
        self._feed = Feed(settings)

    @property
    def feed(self) -> Feed:
        """The feed client, so the dashboard can report how fresh it is."""
        return self._feed

    async def start(self) -> None:
        """Start the feed's background index; it connects only when it walks."""
        await self._feed.start()

    async def place(self, tip: Tip) -> BetResult:
        """Resolve every leg against the feed, then stake it unless dry run is on."""
        async with self._feed.connected():
            return await self._placed(tip)

    async def _placed(self, tip: Tip) -> BetResult:
        """The work itself, with a socket already open around it."""
        offers = await self._feed.resolve(tip)

        for leg, offer in zip(tip.legs, offers, strict=True):
            if offer is None:
                log.info("leg_unresolved", fixture=leg.event, market=leg.market)

                continue

            log.info(
                "leg_resolved",
                fixture=offer.event_name,
                offer_id=offer.offer_id,
                tipster_odds=leg.odds,
                live_odds=offer.odds,
                drop_percent=(
                    None if offer.drop_percent is None else round(offer.drop_percent, 1)
                ),
            )

        result = BetResult(
            tip=tip,
            reference="dry-run" if self._dry_run else "",
            placed_at=utcnow(),
            offers=tuple(offers),
            refusal=self._refuse(tip, offers),
        )

        if not result.accepted:
            log.info(
                "bet_refused",
                refusal=result.refusal,
                legs=len(tip.legs),
                tipster_odds=None if tip.odds is None else round(tip.odds, 3),
            )

            return result

        if self._dry_run:
            log.info(
                "tip_observed",
                legs=len(tip.legs),
                resolved=sum(offer is not None for offer in offers),
            )

            return result

        placeable = [offer for offer in offers if offer is not None]

        await self._feed.authenticate(await mint_ce_session(self._settings))

        answer = await self._feed.place_bet(placeable, tip.stake)

        reference = str(answer.get("betId") or answer.get("id") or "")

        if not reference:
            log.error("bet_not_confirmed", answer=answer)

            return replace(result, refusal="bookmaker did not confirm the bet")

        log.info("bet_accepted", reference=reference, stake=tip.stake)

        return replace(result, reference=reference)

    def _refuse(self, tip: Tip, offers: list[LegOffer | None]) -> str:
        """Say why this tip must not be staked, or "" when it may be."""
        placeable = [offer for offer in offers if offer is not None]

        if len(placeable) != len(offers):
            return f"{len(offers) - len(placeable)} of {len(offers)} legs not in the feed"

        started = [offer for offer in placeable if offer.started]

        if started:
            return f"{started[0].event_name} has already started"

        if tip.message.chat_id in self._forced:
            log.info("checks_forced", chat=tip.message.chat_id)

            return ""

        furthest = max(offer.days_ahead for offer in placeable)

        if furthest > self._max_event_days_ahead:
            limit = self._max_event_days_ahead

            return f"event is {furthest:.1f} days away, limit {limit}"

        live = math.prod(offer.odds for offer in placeable)

        drop = None if tip.odds is None else (tip.odds - live) / tip.odds * 100

        if drop is None:
            log.info("odds_drop_unchecked", live=round(live, 3))
        elif drop > self._max_drop_percent:
            return f"odds dropped {drop:.1f}%, limit {self._max_drop_percent:.0f}%"

        return ""

    async def stop(self) -> None:
        """Close the feed."""
        await self._feed.stop()
