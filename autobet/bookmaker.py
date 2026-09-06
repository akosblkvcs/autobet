"""Resolve a tip against the live feed, then stake it."""

import structlog

from autobet.config import Settings
from autobet.feed import Feed
from autobet.models import BetResult, Tip, utcnow

log = structlog.get_logger(__name__)


class Bookmaker:
    """The one bookmaker. Stakes only when dry run is off."""

    name = "tippmixpro"

    def __init__(self, settings: Settings) -> None:
        """Keep the killswitch and build the feed client."""
        self._dry_run = settings.dry_run
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
            accepted=True,
            reference="dry-run",
            placed_at=utcnow(),
            offers=tuple(offers),
        )

        if self._dry_run:
            log.info(
                "tip_observed",
                legs=len(tip.legs),
                resolved=sum(offer is not None for offer in offers),
            )

            return result

        raise NotImplementedError("placement is stage 4; run with DRY_RUN=True")

    async def stop(self) -> None:
        """Close the feed."""
        await self._feed.stop()
