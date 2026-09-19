"""Resolve a tip against the live feed, then stake it."""

import math
from collections.abc import Sequence
from dataclasses import replace

import structlog

from autobet.config import Settings
from autobet.connection import Connection, connected
from autobet.index import Index
from autobet.models import (
    BetResult,
    LegOffer,
    LegResolution,
    Refusal,
    RefusalCode,
    SelectionStatus,
    Tip,
    utcnow,
)
from autobet.session import mint_ce_session
from autobet.storage import Store

log = structlog.get_logger(__name__)

_MISMATCH_RISE_PERCENT = 44.0


def _priced(tip: Tip, offers: Sequence[LegOffer]) -> tuple[float, float | None]:
    """The whole slip's live price, and how far it sits below the tipster's."""
    live = math.prod(offer.odds for offer in offers)
    drop = None if tip.odds is None else (tip.odds - live) / tip.odds * 100

    return live, drop


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
        resolutions = await self._index.resolve(connection, tip)
        offers = [resolution.offer for resolution in resolutions]

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
            resolutions=tuple(resolutions),
            refusal=self._refuse(tip, offers),
        )

        if result.refusal is not None:
            log.info(
                "bet_refused",
                refusal=result.refusal.code,
                detail=result.refusal.detail,
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
        restated = tuple(
            LegResolution(SelectionStatus.NO_ODDS)
            if offer is None and resolution.offer is not None
            else LegResolution(resolution.status, offer)
            for resolution, offer in zip(resolutions, repriced, strict=True)
        )

        if moved is not None:
            log.info(
                "bet_refused",
                refusal=moved.code,
                detail=moved.detail,
                legs=len(tip.legs),
                late=True,
            )

            return replace(result, refusal=moved, resolutions=restated)

        placeable = [offer for offer in repriced if offer is not None]

        answer = await connection.place_bet(placeable, tip.stake)
        result = replace(result, resolutions=restated)

        reference = str(answer.get("betId") or answer.get("id") or "")

        if not reference:
            log.error("bet_not_confirmed", answer=answer)

            return replace(
                result, refusal=Refusal(RefusalCode.BOOK_REJECTED, "no bet id returned")
            )

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

    def _refuse(self, tip: Tip, offers: list[LegOffer | None]) -> Refusal | None:
        """Say why this tip must not be staked, or None when it may be."""
        mismatched = self._mismatched(tip, offers)

        if mismatched is not None:
            return mismatched

        if tip.message.chat_id in self._forced:
            log.info("checks_forced", chat=tip.message.chat_id)

            return None

        return self._beyond_limits(tip, [o for o in offers if o is not None])

    def _mismatched(self, tip: Tip, offers: list[LegOffer | None]) -> Refusal | None:
        """Signs this is not the bet the tipster sent. A forced chat waives none."""
        placeable = [offer for offer in offers if offer is not None]

        if len(placeable) != len(offers):
            missing = len(offers) - len(placeable)

            return Refusal(RefusalCode.LEG_UNRESOLVED, f"{missing} of {len(offers)} legs")

        started = [offer for offer in placeable if offer.started]

        if started:
            return Refusal(RefusalCode.EVENT_STARTED, started[0].event_name)

        _, drop = _priced(tip, placeable)

        if drop is not None and -drop > _MISMATCH_RISE_PERCENT:
            return Refusal(
                RefusalCode.ODDS_RISE,
                f"{-drop:.1f}% above the tipster, limit {_MISMATCH_RISE_PERCENT:.0f}%",
            )

        return None

    def _beyond_limits(self, tip: Tip, offers: list[LegOffer]) -> Refusal | None:
        """The policy limits, which a forced chat does waive."""
        furthest = max(offer.days_ahead for offer in offers)

        if furthest > self._max_event_days_ahead:
            return Refusal(
                RefusalCode.HORIZON,
                f"{furthest:.1f} days away, limit {self._max_event_days_ahead}",
            )

        live, drop = _priced(tip, offers)

        if drop is None:
            log.info("odds_drop_unchecked", live=round(live, 3))
        elif drop > self._max_drop_percent:
            return Refusal(
                RefusalCode.ODDS_DROP,
                f"{drop:.1f}%, limit {self._max_drop_percent:.0f}%",
            )

        return None
