"""Resolve a tip against the live feed, then stake it."""

import asyncio
import math
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import structlog

from autobet.books import TIPPMIXPRO, Tippmixpro
from autobet.config import Settings
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
from autobet.policy import Policy
from autobet.session import mint_ce_session
from autobet.storage import Store
from autobet.storage.accounts import Account

log = structlog.get_logger(__name__)


_SETTLEMENT_MAP: dict[str, str] = {
    "WON": "won",
    "LOST": "lost",
    "HALF_WON": "half_won",
    "HALF_LOST": "half_lost",
    "VOID": "void",
    "CANCELLED": "void",
    "CANCELED": "void",
    "REJECTED": "void",
    "CASHED_OUT": "cashed_out",
    "CASHOUT": "cashed_out",
    "OPEN": "pending",
    "PENDING": "pending",
    "UNSETTLED": "pending",
}


def _normalize_settlement(raw_status: str) -> str | None:
    """Map the bookmaker API's bet status to our database settlement enum."""
    return _SETTLEMENT_MAP.get(raw_status.upper().strip())


def _extract_settlement(details: dict[str, Any]) -> tuple[str | None, Decimal | None]:
    """Parse settlement outcome and payout from the bookmaker's response."""
    raw_status = str(
        details.get("status")
        or details.get("betStatus")
        or details.get("state")
        or ""
    )
    outcome = _normalize_settlement(raw_status)
    if outcome is None:
        return None, None

    raw_payout = (
        details.get("payout")
        or details.get("winningAmount")
        or details.get("returnAmount")
        or details.get("returned")
    )
    returned = None
    if raw_payout is not None:
        try:
            returned = Decimal(str(raw_payout))
        except Exception:
            pass

    return outcome, returned


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
        self._store = store
        self._index = Index(store)

    async def place(self, tip: Tip, policy: Policy) -> Placement:
        """Resolve the tip once, then stake it through every eligible account."""
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
            refusal=self._refuse(tip, offers, policy),
        )
        accounts = await self._store.accounts.active(TIPPMIXPRO)

        results: list[BetResult] = []

        if not accounts:
            log.info("no_account", legs=len(tip.legs))

            results = [
                replace(
                    shared,
                    refusal=shared.refusal
                    or Refusal(RefusalCode.NO_ACCOUNT, "nobody holds credentials"),
                )
            ]
        elif shared.refusal is not None:
            log.info(
                "bet_refused",
                refusal=shared.refusal.code,
                detail=shared.refusal.detail,
                legs=len(tip.legs),
                tipster_odds=None if tip.odds is None else round(tip.odds, 3),
            )

            results = [replace(shared, user_id=account.user_id) for account in accounts]
        elif self._dry_run:
            live, _ = _priced(tip, [offer for offer in offers if offer is not None])

            log.info(
                "tip_observed",
                legs=len(tip.legs),
                resolved=sum(offer is not None for offer in offers),
                accounts=len(accounts),
                stake=tip.stake,
                live_odds=round(live, 3),
            )

            results = [
                replace(
                    shared,
                    user_id=account.user_id,
                    refusal=Refusal(RefusalCode.DRY_RUN, f"{tip.stake} at {live:.3f}"),
                )
                for account in accounts
            ]
        else:
            staked = await asyncio.gather(
                *(
                    self._staked(book, tip, policy, shared, account)
                    for account in accounts
                ),
                return_exceptions=True,
            )
            results = [
                replace(shared, user_id=account.user_id, error=type(one).__name__)
                if isinstance(one, BaseException)
                else one
                for account, one in zip(accounts, staked, strict=True)
            ]

        return Placement(resolutions=shared.resolutions, results=tuple(results))

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
        policy: Policy,
        shared: BetResult,
        account: Account,
    ) -> BetResult:
        """One account's bet: its own socket, its own session, its own price."""
        result = replace(shared, user_id=account.user_id)

        async with connected(book) as connection:
            await connection.authenticate(await self._minted(book, account))

            repriced = await self._repriced(connection, list(shared.offers))
            moved = self._refuse(tip, repriced, policy)
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
                [offer for offer in repriced if offer is not None], tip.stake
            )

        reference = str(answer.get("betId") or answer.get("id") or "")

        if not reference:
            log.error("bet_not_confirmed", answer=answer, user=account.user_id)

            return replace(
                result, refusal=Refusal(RefusalCode.BOOK_REJECTED, "no bet id returned")
            )

        log.info(
            "bet_accepted", reference=reference, stake=tip.stake, user=account.user_id
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
        self, tip: Tip, offers: list[LegOffer | None], policy: Policy
    ) -> Refusal | None:
        """Say why this tip must not be staked, or None when it may be."""
        mismatched = self._mismatched(tip, offers, policy)

        if mismatched is not None:
            return mismatched

        return self._beyond_limits(tip, [o for o in offers if o is not None], policy)

    def _mismatched(
        self, tip: Tip, offers: list[LegOffer | None], policy: Policy
    ) -> Refusal | None:
        """Signs this is not the bet the tipster sent: correctness, not policy."""
        placeable = [offer for offer in offers if offer is not None]

        if len(placeable) != len(offers):
            missing = len(offers) - len(placeable)

            return Refusal(RefusalCode.LEG_UNRESOLVED, f"{missing} of {len(offers)} legs")

        started = [offer for offer in placeable if offer.started]

        if started:
            return Refusal(RefusalCode.EVENT_STARTED, started[0].event_name)

        _, drop = _priced(tip, placeable)

        if drop is not None and -drop > policy.mismatch_rise_percent:
            return Refusal(
                RefusalCode.ODDS_RISE,
                f"{-drop:.1f}% above the tipster, limit {policy.mismatch_rise_percent}%",
            )

        return None

    def _beyond_limits(
        self, tip: Tip, offers: list[LegOffer], policy: Policy
    ) -> Refusal | None:
        """The limits a per-user setting will override in Phase C."""
        live, drop = _priced(tip, offers)

        if drop is None:
            log.info("odds_drop_unchecked", live=round(live, 3))
        elif drop > policy.max_odds_drop_percent:
            return Refusal(
                RefusalCode.ODDS_DROP,
                f"{drop:.1f}%, limit {policy.max_odds_drop_percent:.0f}%",
            )

        return None

    async def settle_bets(
        self,
        *,
        due_only: bool = True,
        min_age_minutes: int = 60,
    ) -> dict[str, int]:
        """Check status of pending bets at the bookmaker and record outcomes.

        When `due_only=True` (default), only matches that kicked off at least
        `min_age_minutes` ago (default 60m) are queried.
        """
        summary = {
            "checked": 0,
            "won": 0,
            "lost": 0,
            "void": 0,
            "half_won": 0,
            "half_lost": 0,
            "cashed_out": 0,
            "pending": 0,
            "errors": 0,
        }
        pending_bets = await self._store.bets.pending(
            due_only=due_only, min_age_minutes=min_age_minutes
        )
        if not pending_bets:
            log.debug("no_pending_bets_to_settle", due_only=due_only)
            return summary

        try:
            book = await self._store.books.config()
        except Exception as error:
            log.warning("book_not_configured_for_settlement", error=str(error))
            return summary

        accounts = await self._store.accounts.active(TIPPMIXPRO)
        accounts_by_user = {acc.user_id: acc for acc in accounts}

        grouped: dict[int, list[Any]] = {}
        for bet in pending_bets:
            uid = bet["user_id"]
            if uid is not None and uid in accounts_by_user:
                grouped.setdefault(uid, []).append(bet)

        for user_id, bets in grouped.items():
            account = accounts_by_user[user_id]
            try:
                ce_session = await self._minted(book, account)
                async with connected(book) as connection:
                    await connection.authenticate(ce_session)

                    for bet in bets:
                        summary["checked"] += 1
                        ref = bet["reference"]
                        try:
                            details = await connection.bet_details(ref)
                            outcome, returned = _extract_settlement(details)

                            if outcome and outcome != "pending":
                                if returned is None and outcome in ("won", "half_won"):
                                    stake = Decimal(str(bet["stake"]))
                                    odds = (
                                        Decimal(str(bet["odds"]))
                                        if bet["odds"]
                                        else Decimal("1")
                                    )
                                    factor = (
                                        Decimal("0.5")
                                        if outcome == "half_won"
                                        else Decimal("1")
                                    )
                                    returned = (stake * odds * factor).quantize(
                                        Decimal("0.01")
                                    )
                                elif outcome in ("lost", "half_lost"):
                                    returned = Decimal("0.00")

                                await self._store.bets.settle(
                                    bet["id"], outcome, returned
                                )
                                if outcome in summary:
                                    summary[outcome] += 1
                                else:
                                    summary["void"] += 1

                                log.info(
                                    "bet_settled",
                                    bet_id=bet["id"],
                                    reference=ref,
                                    outcome=outcome,
                                    returned=str(returned),
                                    user=user_id,
                                )
                            else:
                                summary["pending"] += 1
                        except Exception as error:
                            summary["errors"] += 1
                            log.warning(
                                "bet_settle_failed", reference=ref, error=str(error)
                            )
            except Exception as error:
                summary["errors"] += len(bets)
                log.warning("account_settle_failed", user=user_id, error=str(error))

        return summary
