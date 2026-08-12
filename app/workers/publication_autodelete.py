from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.publication_autodelete import (
    PublicationAutodeleteService,
    PublicationAutodeleteSyncConflict,
)
from app.services.publication_autodelete_candidates import (
    PublicationAutodeleteCandidateSelector,
)
from app.services.publication_autodelete_lease import (
    DEFAULT_PUBLICATION_AUTODELETE_LEASE_SECONDS,
    PublicationAutodeleteLeaseHandle,
    PublicationAutodeleteLeaseService,
)


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteWorkerTick:
    selected: int = 0
    leased: int = 0
    busy: int = 0
    deleted: int = 0
    already_deleted: int = 0
    not_due: int = 0
    ineligible: int = 0
    retry: int = 0
    ambiguous: int = 0
    conflicts: int = 0
    failures: int = 0
    release_failures: int = 0
    cursor: int = 0


class PublicationAutodeleteWorker:
    """Bounded lease-backed worker for canonical-only publication deletion.

    Candidate selection, lease ownership, destructive operation and lease release use
    separate sessions. The operation receives the exact acquired lease handle; durable
    per-message action reservations then remain the no-replay authority even after the
    publication lease is released or expires. Cancellation leaves the publication lease
    until expiry while the action ledger preserves any already-reserved ambiguity.

    ``ambiguous`` is intentionally separate from ``retry``. It represents a provider
    boundary that may already have produced an irreversible side effect and therefore
    must never be interpreted as authorization for another Telegram delete attempt.
    """

    def __init__(
        self,
        *,
        provider,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: int = 60,
        batch_size: int = 25,
        lease_ttl_seconds: int = DEFAULT_PUBLICATION_AUTODELETE_LEASE_SECONDS,
    ) -> None:
        self.provider = provider
        self.session_factory = session_factory
        self.batch_size = max(1, min(int(batch_size), 200))
        self.lease_ttl_seconds = max(30, min(int(lease_ttl_seconds), 600))
        self._holder = f"publication-autodelete-{uuid.uuid4().hex[:16]}"
        self._cursor = 0
        self._loop = PollingLoop(
            interval_seconds=max(30, int(interval_seconds)),
            on_tick=self._tick,
            name="publication-autodelete",
        )

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def _select(self):
        async with self.session_factory() as session:
            return await PublicationAutodeleteCandidateSelector(session).select_batch(
                after_publication_id=self._cursor,
                limit=self.batch_size,
            )

    async def _acquire(
        self,
        publication_id: int,
    ) -> PublicationAutodeleteLeaseHandle | None:
        async with self.session_factory() as session:
            return await PublicationAutodeleteLeaseService(session).acquire(
                publication_id=publication_id,
                holder=self._holder,
                ttl_seconds=self.lease_ttl_seconds,
            )

    async def _release(self, handle: PublicationAutodeleteLeaseHandle) -> bool:
        async with self.session_factory() as session:
            return await PublicationAutodeleteLeaseService(session).release(handle)

    async def run_once(self) -> PublicationAutodeleteWorkerTick:
        batch = await self._select()
        next_cursor = 0 if batch.done else int(batch.next_cursor)
        self._cursor = next_cursor

        leased = 0
        busy = 0
        deleted = 0
        already_deleted = 0
        not_due = 0
        ineligible = 0
        retry = 0
        ambiguous = 0
        conflicts = 0
        failures = 0
        release_failures = 0

        for publication_id in batch.publication_ids:
            try:
                handle = await self._acquire(int(publication_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Publication autodelete: lease acquire failed publication_id={} type={}",
                    int(publication_id),
                    type(exc).__name__,
                )
                continue

            if handle is None:
                busy += 1
                continue
            leased += 1
            release_after = True

            try:
                async with self.session_factory() as operation_session:
                    result = await PublicationAutodeleteService(
                        operation_session,
                        provider=self.provider,
                        allow_report=True,
                    ).delete_if_due(
                        int(publication_id),
                        lease=handle,
                    )
                if result.outcome == "deleted":
                    deleted += 1
                elif result.outcome == "already_deleted":
                    already_deleted += 1
                elif result.outcome == "not_due":
                    not_due += 1
                elif result.outcome == "ineligible":
                    ineligible += 1
                elif result.outcome == "ambiguous":
                    ambiguous += 1
                else:
                    retry += 1
            except asyncio.CancelledError:
                # Do not explicitly release an ambiguous in-flight provider attempt.
                # Expiry is safe and makes cancellation equivalent to process death.
                release_after = False
                raise
            except PublicationAutodeleteSyncConflict:
                conflicts += 1
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Publication autodelete: operation failed publication_id={} type={}",
                    int(publication_id),
                    type(exc).__name__,
                )
            finally:
                if release_after:
                    try:
                        released = await self._release(handle)
                        if not released:
                            release_failures += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        release_failures += 1
                        logger.warning(
                            "Publication autodelete: lease release failed publication_id={} type={}",
                            int(publication_id),
                            type(exc).__name__,
                        )

        return PublicationAutodeleteWorkerTick(
            selected=len(batch.publication_ids),
            leased=leased,
            busy=busy,
            deleted=deleted,
            already_deleted=already_deleted,
            not_due=not_due,
            ineligible=ineligible,
            retry=retry,
            ambiguous=ambiguous,
            conflicts=conflicts,
            failures=failures,
            release_failures=release_failures,
            cursor=self._cursor,
        )

    async def _tick(self) -> None:
        tick = await self.run_once()
        if (
            tick.deleted
            or tick.retry
            or tick.ambiguous
            or tick.conflicts
            or tick.failures
            or tick.release_failures
        ):
            logger.info(
                "Publication autodelete: selected={} leased={} busy={} deleted={} "
                "already_deleted={} not_due={} ineligible={} retry={} ambiguous={} "
                "conflicts={} failures={} release_failures={} cursor={}",
                tick.selected,
                tick.leased,
                tick.busy,
                tick.deleted,
                tick.already_deleted,
                tick.not_due,
                tick.ineligible,
                tick.retry,
                tick.ambiguous,
                tick.conflicts,
                tick.failures,
                tick.release_failures,
                tick.cursor,
            )
