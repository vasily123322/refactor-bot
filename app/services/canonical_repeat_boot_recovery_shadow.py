from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Literal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.services.canonical_repeat_boot_recovery_reservation import (
    CanonicalRepeatBootRecoveryReservationService,
)
from app.services.canonical_repeat_boot_recovery_verifier import (
    CanonicalRepeatBootRecoveryVerifier,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatBootRecoveryShadowGroup:
    repeat_group_id: int
    source_publication_ids: tuple[int, ...]
    reservation_outcome: Literal[
        "reserved",
        "already_reserved",
        "existing_successor",
        "ineligible",
        "conflict",
        "failed",
    ]
    verification_outcome: Literal[
        "matched",
        "pending",
        "ineligible",
        "conflict",
        "failed",
    ] | None = None


@dataclass(frozen=True, slots=True)
class CanonicalRepeatBootRecoveryShadowResult:
    remaining: tuple[PostTask, ...]
    groups: tuple[CanonicalRepeatBootRecoveryShadowGroup, ...]


class CanonicalRepeatBootRecoveryShadowCoordinator:
    """Observe one legacy boot cleanup batch without becoming its dependency.

    Eligible overdue repeat tasks are grouped using the same legacy repeat-group marker
    and selected-order semantics. Canonical reservation is best-effort before the
    authoritative callback. The callback is always invoked exactly once unless it
    raises/cancels itself. Verification happens only after that full callback, which
    lets ``PublicationScheduler`` perform its normal child mirror and source projection.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _group_id(post: PostTask) -> int | None:
        payload = dict(post.payload or {})
        if payload.get("repeat_on") is not True:
            return None
        raw_group = payload.get("repeat_group_id")
        try:
            group_id = int(raw_group) if raw_group is not None else int(post.id)
        except (TypeError, ValueError, OverflowError):
            return None
        return group_id if group_id > 0 else None

    async def _source_publication_id(self, post: PostTask) -> int | None:
        publication = await LegacyPublicationBridge(self.session).reconcile_task(post)
        if publication is None:
            publication = await mirror_legacy_post_task(self.session, post)
        return int(publication.id) if publication is not None else None

    async def _reserve_groups(
        self,
        items: list[PostTask],
        *,
        after: datetime,
    ) -> list[CanonicalRepeatBootRecoveryShadowGroup]:
        grouped: dict[int, list[int]] = {}
        ordered_groups: list[int] = []
        current = as_utc(after)
        for post in items:
            if post.scheduled_at is None or as_utc(post.scheduled_at) > current:
                continue
            group_id = self._group_id(post)
            if group_id is None:
                continue
            try:
                publication_id = await self._source_publication_id(post)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Canonical boot recovery shadow source mirror failed post_id={} type={}",
                    int(post.id),
                    type(exc).__name__,
                )
                continue
            if publication_id is None:
                continue
            if group_id not in grouped:
                grouped[group_id] = []
                ordered_groups.append(group_id)
            grouped[group_id].append(publication_id)

        results: list[CanonicalRepeatBootRecoveryShadowGroup] = []
        for group_id in ordered_groups:
            source_ids = tuple(grouped[group_id])
            try:
                reservation = await CanonicalRepeatBootRecoveryReservationService(
                    self.session
                ).reserve_group(source_ids, after=current)
                outcome = reservation.outcome
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outcome = "failed"
                logger.warning(
                    "Canonical boot recovery shadow reservation failed group_id={} type={}",
                    group_id,
                    type(exc).__name__,
                )
            results.append(
                CanonicalRepeatBootRecoveryShadowGroup(
                    repeat_group_id=group_id,
                    source_publication_ids=source_ids,
                    reservation_outcome=outcome,  # type: ignore[arg-type]
                )
            )
        return results

    async def run(
        self,
        *,
        items: list[PostTask],
        after: datetime,
        legacy_cleanup: Callable[[], Awaitable[list[PostTask]]],
    ) -> CanonicalRepeatBootRecoveryShadowResult:
        groups = await self._reserve_groups(items, after=after)

        remaining = await legacy_cleanup()

        verified: list[CanonicalRepeatBootRecoveryShadowGroup] = []
        for group in groups:
            verification_outcome: str | None = None
            if group.reservation_outcome in {"reserved", "already_reserved"}:
                try:
                    verification = await CanonicalRepeatBootRecoveryVerifier(
                        self.session
                    ).verify(group.source_publication_ids)
                    verification_outcome = verification.outcome
                    if verification.outcome == "matched":
                        logger.debug(
                            "Canonical boot recovery shadow matched group_id={} sources={} "
                            "successor_publication_id={}",
                            group.repeat_group_id,
                            len(group.source_publication_ids),
                            verification.successor_publication_id,
                        )
                    else:
                        logger.warning(
                            "Canonical boot recovery shadow mismatch group_id={} sources={} "
                            "outcome={}",
                            group.repeat_group_id,
                            len(group.source_publication_ids),
                            verification.outcome,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    verification_outcome = "failed"
                    logger.warning(
                        "Canonical boot recovery shadow verification failed group_id={} "
                        "type={}",
                        group.repeat_group_id,
                        type(exc).__name__,
                    )
            verified.append(
                CanonicalRepeatBootRecoveryShadowGroup(
                    repeat_group_id=group.repeat_group_id,
                    source_publication_ids=group.source_publication_ids,
                    reservation_outcome=group.reservation_outcome,
                    verification_outcome=verification_outcome,  # type: ignore[arg-type]
                )
            )

        return CanonicalRepeatBootRecoveryShadowResult(
            remaining=tuple(remaining),
            groups=tuple(verified),
        )
