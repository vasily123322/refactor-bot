from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger

from app.services.publication_autodelete_views import PublicationAutodeleteViewsSyncConflict
from app.services.publication_autodelete_views_composed import (
    PublicationAutodeleteViewsComposedService,
)
from app.workers.publication_autodelete_views import (
    PublicationAutodeleteViewsWorkerTick,
    _utc,
)
from app.workers.publication_autodelete_views_pin import PublicationAutodeleteViewsPinWorker


class PublicationAutodeleteViewsForwardWorker(PublicationAutodeleteViewsPinWorker):
    """Started-only repeat+views+ordered-forward destructive dependency.

    This worker keeps the previously proven plain repeat+views and views+pin facts
    independent while adding a third default-off views+forward fact. Construction alone
    never publishes availability. `repeat_views_forward_available` opens only after the
    exact worker starts successfully and plain repeat+views is also available.

    Its operation loop reuses the same exact lease/select/release/backoff orchestration and
    current #300 composed destructive service; the new bit changes only provider-free
    lifecycle admission for views+forward.
    """

    def __init__(
        self,
        *args,
        allow_repeat_views_forward: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.allow_repeat_views_forward = bool(allow_repeat_views_forward)

    @property
    def repeat_views_forward_available(self) -> bool:
        return bool(
            self._started
            and self.allow_repeat_views
            and self.allow_repeat_views_forward
        )

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
                    "Publication views+forward autodelete: lease acquire failed "
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
                    result = await PublicationAutodeleteViewsComposedService(
                        operation_session,
                        view_source=self.view_source,
                        delete_provider=self.delete_provider,
                        next_check_seconds=self.next_check_seconds,
                        allow_report=True,
                        allow_repeat_views=self.allow_repeat_views,
                        allow_repeat_views_pin=self.allow_repeat_views_pin,
                        allow_repeat_views_forward=self.allow_repeat_views_forward,
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
                                "Publication views+forward autodelete: ineligible backoff failed "
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
                                    "Publication views+forward autodelete: ambiguity backoff failed "
                                    "publication_id={} type={}",
                                    int(publication_id),
                                    type(exc).__name__,
                                )
            except asyncio.CancelledError:
                release_after = False
                raise
            except PublicationAutodeleteViewsSyncConflict:
                conflicts += 1
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Publication views+forward autodelete: operation failed "
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
                            "Publication views+forward autodelete: lease release failed "
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
