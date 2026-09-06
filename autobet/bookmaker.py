"""Resolve a tip against the live feed, then stake it."""

import math

import structlog

from autobet.config import Settings
from autobet.feed import Feed
from autobet.models import BetResult, LegOffer, Tip, utcnow

log = structlog.get_logger(__name__)


class Bookmaker:
    """The one bookmaker. Stakes only when dry run is off."""

    name = "tippmixpro"

    def __init__(self, settings: Settings) -> None:
        """Keep the killswitches and build the feed client."""
        self._dry_run = settings.dry_run
        self._max_drop_percent = settings.max_odds_drop_percent
        self._feed = Feed()

    async def start(self) -> None:
        """Connect the feed; its event index fills in behind us."""
        await self._feed.start()

    async def place(self, tip: Tip) -> BetResult:
        """Resolve every leg against the feed, then stake it unless dry run is on."""
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
                drop_percent=round(offer.drop_percent, 1),
            )

        result = BetResult(
            tip=tip,
            reference="dry-run",
            placed_at=utcnow(),
            offers=tuple(offers),
            refusal=self._refuse(tip, offers),
        )

        if not result.accepted:
            log.info(
                "bet_refused",
                refusal=result.refusal,
                legs=len(tip.legs),
                tipster_odds=round(tip.odds, 3),
            )

            return result

        if self._dry_run:
            log.info(
                "tip_observed",
                legs=len(tip.legs),
                resolved=sum(offer is not None for offer in offers),
            )

            return result

        raise NotImplementedError("placement is stage 4; run with DRY_RUN=True")

    def _refuse(self, tip: Tip, offers: list[LegOffer | None]) -> str:
        """Say why this tip must not be staked, or "" when it may be."""
        placeable = [offer for offer in offers if offer is not None]

        if len(placeable) != len(offers):
            return f"{len(offers) - len(placeable)} of {len(offers)} legs not in the feed"

        live = math.prod(offer.odds for offer in placeable)
        drop = (tip.odds - live) / tip.odds * 100

        if drop > self._max_drop_percent:
            return f"odds dropped {drop:.1f}%, limit {self._max_drop_percent:.0f}%"

        return ""

    async def stop(self) -> None:
        """Close the feed."""
        await self._feed.stop()
