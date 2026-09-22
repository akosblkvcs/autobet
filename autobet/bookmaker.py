"""Resolve a tip against the live feed, then stake it."""

import asyncio
import math
from collections.abc import Sequence
from dataclasses import replace

import structlog

from autobet.books import TIPPMIXPRO, Tippmixpro
from autobet.connection import Connection, connected
from autobet.index import Index
from autobet.models import (
    BetResult,
    LegOffer,
    LegResolution,
    Placement,
    Refusal,
    RefusalCode,
    SelectionStatus,
    Tip,
    utcnow,
)
from autobet.policy import Mode, Policy, Terms
from autobet.session import mint_ce_session
from autobet.storage import Store
from autobet.storage.accounts import Account

log = structlog.get_logger(__name__)


def _priced(tip: Tip, offers: Sequence[LegOffer]) -> tuple[float, float | None]:
    """The whole slip's live price, and how far it sits below the tipster's."""
    live = math.prod(offer.odds for offer in offers)
    drop = None if tip.odds is None else (tip.odds - live) / tip.odds * 100

    return live, drop


class Bookmaker:
    """The bookmaker representation."""

    def __init__(self, store: Store) -> None:
        """Hold the store and build the index the tips are resolved against."""
        self._store = store
        self._index = Index(store)

    async def place(self, tip: Tip, policy: Policy) -> Placement:
        """Resolve the tip once, then stake it on each account's own terms."""
        book = await self._store.books.config()

        async with connected(book) as connection:
            resolutions = await self._index.resolve(connection, tip)

        offers = [resolution.offer for resolution in resolutions]

        self._report(tip, offers)

        shared = BetResult(
            tip=tip,
            reference="",
            placed_at=utcnow(),
            resolutions=tuple(resolutions),
            refusal=self._mismatched(tip, offers),
        )
        accounts = await self._store.accounts.active(TIPPMIXPRO)

        log.info(
            "tip_resolved",
            legs=len(tip.legs),
            resolved=sum(offer is not None for offer in offers),
            accounts=len(accounts),
            refusal=None if shared.refusal is None else shared.refusal.code,
            tipster_odds=None if tip.odds is None else round(tip.odds, 3),
        )

        if not accounts:
            return Placement(
                resolutions=shared.resolutions,
                results=(
                    replace(
                        shared,
                        refusal=shared.refusal
                        or Refusal(RefusalCode.NO_ACCOUNT, "nobody holds credentials"),
                    ),
                ),
            )

        held = await self._store.users.policies([account.user_id for account in accounts])
        terms = [held[account.user_id].over(policy) for account in accounts]
        bets = await asyncio.gather(
            *(
                self._bet(book, tip, shared, own, account)
                for own, account in zip(terms, accounts, strict=True)
            ),
            return_exceptions=True,
        )

        return Placement(
            resolutions=shared.resolutions,
            results=tuple(
                self._blown(shared, account, own, one)
                if isinstance(one, BaseException)
                else one
                for account, own, one in zip(accounts, terms, bets, strict=True)
            ),
        )

    def _blown(
        self, shared: BetResult, account: Account, terms: Terms, error: BaseException
    ) -> BetResult:
        """One account's bet raised: the row says which, the log says why."""
        log.error("bet_failed", user=account.user_id, exc_info=error)

        return replace(
            shared,
            user_id=account.user_id,
            stake=terms.stake,
            error=type(error).__name__,
        )

    async def _bet(
        self,
        book: Tippmixpro,
        tip: Tip,
        shared: BetResult,
        terms: Terms,
        account: Account,
    ) -> BetResult:
        """One account's verdict: the tip's own faults, then this person's terms."""
        result = replace(shared, user_id=account.user_id, stake=terms.stake)

        if shared.refusal is not None:
            return result

        if terms.paused:
            return replace(
                result, refusal=Refusal(RefusalCode.USER_PAUSED, "paused by the user")
            )

        placeable = [offer for offer in shared.offers if offer is not None]
        beyond = self._beyond_limits(tip, placeable, terms)

        if beyond is not None:
            return replace(result, refusal=beyond)

        live, _ = _priced(tip, placeable)
        unsent = f"{terms.stake} at {live:.3f}"

        if terms.mode is Mode.PAPER:
            return replace(result, refusal=Refusal(RefusalCode.PAPER_MODE, unsent))

        return await self._staked(book, tip, terms, shared, account)

    def _report(self, tip: Tip, offers: list[LegOffer | None]) -> None:
        """Say what each leg resolved to, once, however many accounts follow."""
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

    async def _staked(
        self,
        book: Tippmixpro,
        tip: Tip,
        terms: Terms,
        shared: BetResult,
        account: Account,
    ) -> BetResult:
        """One account's bet: its own socket, its own session, its own price."""
        result = replace(shared, user_id=account.user_id, stake=terms.stake)

        async with connected(book) as connection:
            await connection.authenticate(await self._minted(book, account))

            repriced = await self._repriced(connection, list(shared.offers))
            moved = self._refuse(tip, repriced, terms)
            restated = tuple(
                LegResolution(SelectionStatus.NO_ODDS)
                if offer is None and resolution.offer is not None
                else LegResolution(resolution.status, offer)
                for resolution, offer in zip(shared.resolutions, repriced, strict=True)
            )
            result = replace(result, resolutions=restated)

            if moved is not None:
                log.info(
                    "bet_refused",
                    refusal=moved.code,
                    detail=moved.detail,
                    user=account.user_id,
                    late=True,
                )

                return replace(result, refusal=moved)

            answer = await connection.place_bet(
                [offer for offer in repriced if offer is not None], terms.stake
            )

        reference = str(answer.get("betId") or answer.get("id") or "")

        if not reference:
            log.error("bet_not_confirmed", answer=answer, user=account.user_id)

            return replace(
                result, refusal=Refusal(RefusalCode.BOOK_REJECTED, "no bet id returned")
            )

        log.info(
            "bet_accepted", reference=reference, stake=terms.stake, user=account.user_id
        )

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

    async def _minted(self, book: Tippmixpro, account: Account) -> str:
        """A betting session for one account, from credentials held that long."""
        username, password = await self._store.accounts.credentials(account.id)

        return await mint_ce_session(book, username, password)

    def _refuse(
        self, tip: Tip, offers: list[LegOffer | None], terms: Terms
    ) -> Refusal | None:
        """Say why this person must not stake this tip, or None when they may."""
        mismatched = self._mismatched(tip, offers)

        if mismatched is not None:
            return mismatched

        return self._beyond_limits(tip, [o for o in offers if o is not None], terms)

    def _mismatched(self, tip: Tip, offers: list[LegOffer | None]) -> Refusal | None:
        """Signs this is not the bet the tipster sent: correctness, not policy."""
        placeable = [offer for offer in offers if offer is not None]

        if len(placeable) != len(offers):
            missing = len(offers) - len(placeable)

            return Refusal(RefusalCode.LEG_UNRESOLVED, f"{missing} of {len(offers)} legs")

        started = [offer for offer in placeable if offer.started]

        if started:
            return Refusal(RefusalCode.EVENT_STARTED, started[0].event_name)

        if tip.odds is None:
            return Refusal(RefusalCode.UNPRICED, "the tipster quoted no price")

        return None

    def _beyond_limits(
        self, tip: Tip, offers: list[LegOffer], terms: Terms
    ) -> Refusal | None:
        """The band this person's price must fall in, theirs to widen or narrow."""
        live, drop = _priced(tip, offers)
        assert tip.odds is not None  # `_mismatched` refuses an unpriced tip first
        assert drop is not None
        prices = f"{live:.3f} against {tip.odds:.3f}"

        if drop > terms.max_odds_drop_percent:
            return Refusal(
                RefusalCode.ODDS_DROP,
                f"{prices}, {drop:.1f}% below, limit {terms.max_odds_drop_percent:g}%",
            )

        if -drop > terms.max_odds_rise_percent:
            return Refusal(
                RefusalCode.ODDS_RISE,
                f"{prices}, {-drop:.1f}% above, limit {terms.max_odds_rise_percent:g}%",
            )

        return None
