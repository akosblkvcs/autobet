"""Resolve a tip against the live feed, then stake it."""

import math
from dataclasses import replace

import structlog

from autobet.config import Settings
from autobet.connection import Connection, connected
from autobet.index import Index
from autobet.models import BetResult, LegOffer, Tip, utcnow
from autobet.session import mint_ce_session
from autobet.storage import Store

log = structlog.get_logger(__name__)


class Bookmaker:
    """The bookmaker representation."""

    def __init__(self, settings: Settings, store: Store) -> None:
        """Store the settings and build the index the tips are resolved against."""
        self._settings = settings
        self._dry_run = settings.dry_run
        self._max_drop_percent = settings.max_odds_drop_percent
        self._max_event_days_ahead = settings.max_event_days_ahead
        self._forced = set(settings.telegram_force_chat_ids)
        self._index = Index(settings, store)

    async def place(self, tip: Tip) -> BetResult:
        """Resolve every leg against the feed, then stake it."""
        async with connected(self._settings) as connection:
            return await self._placed(connection, tip)

    async def _placed(self, connection: Connection, tip: Tip) -> BetResult:
        """The work itself, on the socket opened for this tip."""
        offers = await self._index.resolve(connection, tip)

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

        await connection.authenticate(await mint_ce_session(self._settings))

        repriced = await self._repriced(connection, offers)
        moved = self._refuse(tip, repriced)

        if moved:
            log.info("bet_refused", refusal=moved, legs=len(tip.legs), late=True)

            return replace(result, refusal=moved, offers=tuple(repriced))

        placeable = [offer for offer in repriced if offer is not None]

        answer = await connection.place_bet(placeable, tip.stake)

        reference = str(answer.get("betId") or answer.get("id") or "")

        if not reference:
            log.error("bet_not_confirmed", answer=answer)

            return replace(result, refusal="bookmaker did not confirm the bet")

        log.info("bet_accepted", reference=reference, stake=tip.stake)

        return replace(result, reference=reference)

    async def _repriced(
        self, connection: Connection, offers: list[LegOffer | None]
    ) -> list[LegOffer | None]:
        """The same offers at today's price, or None where the book dropped one."""
        resolved = [offer for offer in offers if offer is not None]
        live = await connection.prices([offer.offer_id for offer in resolved])

        return [
            replace(offer, odds=live[offer.offer_id])
            if offer is not None and offer.offer_id in live
            else None
            for offer in offers
        ]

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
