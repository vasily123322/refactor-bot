from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.publication_autodelete_lease import (
    DEFAULT_PUBLICATION_AUTODELETE_LEASE_SECONDS,
    PublicationAutodeleteLeaseHandle,
    PublicationAutodeleteLeaseService,
)
from app.services.publication_autodelete_views import (
    PublicationAutodeleteViewsService,
    PublicationAutodeleteViewsSyncConflict,
)
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_mixed_autodelete import (
    PublicationMixedAutodeleteService,
    PublicationMixedAutodeleteSyncConflict,
)


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewsWorkerTick:
    selected: int = 0
    leased: int = 0
    busy: int = 0
    deleted: int = 0
    already_deleted: int = 0
    below_threshold: int = 0
    deferred: int = 0
    not_due: int = 0
    ineligible: int = 0
    retry: int = 0
    ambiguous: int = 0
    conflicts: int = 0
    failures: int = 0
    release_failures: int = 0
    backoff_failures: int = 0


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


class PublicationAutodeleteViewsWorker:
    """Bounded lease-backed orchestrator for views-based canonical deletion.

    Repeat evaluation is an explicit construction-time capability and is independently
    default-off. `repeat_views_available` becomes true only after this exact worker has
    successfully started; configuration or construction alone is never an availability
    proof. Every destructive call still goes through the durable reserve-before-DELETE
    service barrier with the exact acquired lease handle.
    """

    def __init__(
        self,
        *,
        view_source,
        delete_provider,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: int = 60,
        batch_size: int = 25,
        lease_ttl_seconds: int = DEFAULT_PUBLICATION_AUTODELETE_LEASE_SECONDS,
        next_check_seconds: int = 60,
        ineligible_backoff_seconds: int = 300,
        allow_repeat_views: bool = False,
    ) -> None:
        self.view_source = view_source
        self.delete_provider = delete_provider
        self.session_factory = session_factory
        self.batch_size = max(1, min(int(batch_size), 200))
        self.lease_ttl_seconds = max(30, min(int(lease_ttl_seconds), 600))
        self.next_check_seconds = max(15, min(int(next_check_seconds), 3600))
        self.ineligible_backoff_seconds = max(
            30, min(int(ineligible_backoff_seconds), 3600)
        )
        self.allow_repeat_views = bool(allow_repeat_views)
        self._started = False
        self._holder = f"publication-autodelete-views-{uuid.uuid4().hex[:16]}"
        self._loop = PollingLoop(
            interval_seconds=max(30, int(interval_seconds)),
            on_tick=self._tick,
            name="publication-autodelete-views",
        )

    @property
    def repeat_views_available(self) -> bool:
        return bool(self._started and self.allow_repeat_views)

    async def start(self) -> None:
        await self._loop.start()
        self._started = True

    async def stop(self) -> None:
        try:
            await self._loop.stop()
        finally:
            self._started = False

    async def _select(self, *, now: datetime):
        async with self.session_factory() as session:
            return await PublicationAutodeleteViewStateService(
                session
            ).select_due_publication_ids(
                now=now,
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
                allow_linked=True,
            )

    async def _release(self, handle: PublicationAutodeleteLeaseHandle) -> bool:
        async with self.session_factory() as session:
            return await PublicationAutodeleteLeaseService(session).release(handle)

    async def _backoff_ineligible(
        self,
        *,
        publication_id: int,
        threshold: int,
        now: datetime,
    ) -> bool:
        async with self.session_factory() as session:
            snapshot = await PublicationAutodeleteViewStateService(
                session
            ).defer_if_current(
                publication_id=publication_id,
                expected_threshold=threshold,
                next_check_at=now + timedelta(seconds=self.ineligible_backoff_seconds),
            )
            if snapshot is None:
                await session.rollback()
                return False
            await session.commit()
            return True

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> PublicationAutodeleteViewsWorkerTick:
        current = _utc(now)
        batch = await self._select(now=current)

        leased = 0
        busy = 0
        deleted = 0
        already_deleted = 0
        below_threshold = 0
        deferred = 0
        not_due = 0
        ineligible = 0
        retry = 0
        ambiguous = 0
        conflicts = 0
        failures = 0
        release_failures = 0
        backoff_failures = 0

        for publication_id in batch.publication_ids:
            try:
                handle = await self._acquire(int(publication_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Publication views autodelete: lease acquire failed "
                    "publication_id={} type={}",
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
                    result = await PublicationMixedAutodeleteService(
                        operation_session,
                        view_source=self.view_source,
                        delete_provider=self.delete_provider,
                        next_check_seconds=self.next_check_seconds,
                        allow_report=True,
                    ).views_evaluate_and_delete(
                        int(publication_id),
                        lease=handle,
                        now=current,
                    )
                    if result is None:
                        result = await PublicationAutodeleteViewsService(
                            operation_session,
                            view_source=self.view_source,
                            delete_provider=self.delete_provider,
                            next_check_seconds=self.next_check_seconds,
                            allow_report=True,
                            allow_repeat_views=self.allow_repeat_views,
                            lease=handle,
                        ).evaluate_and_delete(int(publication_id), now=current)

                if result.outcome == "deleted":
                    deleted += 1
                elif result.outcome == "already_deleted":
                    already_deleted += 1
                elif result.outcome == "below_threshold":
                    below_threshold += 1
                elif result.outcome == "deferred":
                    deferred += 1
                elif result.outcome == "not_due":
                    not_due += 1
                elif result.outcome == "ineligible":
                    ineligible += 1
                    if result.threshold is not None:
                        try:
                            backed_off = await self._backoff_ineligible(
                                publication_id=int(publication_id),
                                threshold=int(result.threshold),
                                now=current,
                            )
                            if not backed_off:
                                backoff_failures += 1
                        except asyncio.CancelledError:
                            release_after = False
                            raise
                        except Exception as exc:
                            backoff_failures += 1
                            logger.warning(
                                "Publication views autodelete: ineligible backoff failed "
                                "publication_id={} type={}",
                                int(publication_id),
                                type(exc).__name__,
                            )
                else:
                    retry += 1
                    if result.ambiguous_count:
                        ambiguous += 1
                        if result.threshold is not None:
                            try:
                                backed_off = await self._backoff_ineligible(
                                    publication_id=int(publication_id),
                                    threshold=int(result.threshold),
                                    now=current,
                                )
                                if not backed_off:
                                    backoff_failures += 1
                            except asyncio.CancelledError:
                                release_after = False
                                raise
                            except Exception as exc:
                                backoff_failures += 1
                                logger.warning(
                                    "Publication views autodelete: ambiguity backoff failed "
                                    "publication_id={} type={}",
                                    int(publication_id),
                                    type(exc).__name__,
                                )
            except asyncio.CancelledError:
                release_after = False
                raise
            except (
                PublicationAutodeleteViewsSyncConflict,
                PublicationMixedAutodeleteSyncConflict,
            ):
                conflicts += 1
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Publication views autodelete: operation failed "
                    "publication_id={} type={}",
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
                            "Publication views autodelete: lease release failed "
                            "publication_id={} type={}",
                            int(publication_id),
                            type(exc).__name__,
                        )

        return PublicationAutodeleteViewsWorkerTick(
            selected=len(batch.publication_ids),
            leased=leased,
            busy=busy,
            deleted=deleted,
            already_deleted=already_deleted,
            below_threshold=below_threshold,
            deferred=deferred,
            not_due=not_due,
            ineligible=ineligible,
            retry=retry,
            ambiguous=ambiguous,
            conflicts=conflicts,
            failures=failures,
            release_failures=release_failures,
            backoff_failures=backoff_failures,
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
            or tick.backoff_failures
        ):
            logger.info(
                "Publication views autodelete: selected={} leased={} busy={} deleted={} "
                "already_deleted={} below_threshold={} deferred={} not_due={} "
                "ineligible={} retry={} ambiguous={} conflicts={} failures={} "
                "release_failures={} backoff_failures={}",
                tick.selected,
                tick.leased,
                tick.busy,
                tick.deleted,
                tick.already_deleted,
                tick.below_threshold,
                tick.deferred,
                tick.not_due,
                tick.ineligible,
                tick.retry,
                tick.ambiguous,
                tick.conflicts,
                tick.failures,
                tick.release_failures,
                tick.backoff_failures,
            )
