from __future__ import annotations

from loguru import logger

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.legacy_content_mirror import mirror_unlinked_legacy_tasks
from app.services.publication_autodelete_views_backfill import (
    PublicationAutodeleteViewsBackfillService,
)
from app.services.publication_autodelete_views_legacy_sync import (
    sync_active_legacy_view_intents,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import PublicationRuntimeProjector


class PublicationReconcilerWorker:
    """Keep the new content/publication domain synchronized during migration."""

    def __init__(self, *, interval_seconds: int = 5, batch_size: int = 100):
        self.batch_size = max(1, min(int(batch_size), 500))
        self._runtime_backfill_cursor = 0
        self._runtime_backfill_done = False
        self._views_backfill_cursor = 0
        self._views_backfill_done = False
        self._loop = PollingLoop(
            interval_seconds=max(1, int(interval_seconds)),
            on_tick=self._tick,
            name="publication-reconciler",
        )

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def _tick(self) -> None:
        runtime_scanned = 0
        runtime_updated = 0
        view_sync_scanned = 0
        view_sync_synced = 0
        view_sync_cleared = 0
        view_sync_invalid = 0
        views_backfill_scanned = 0
        views_backfill_synced = 0
        views_backfill_cleared = 0
        views_backfill_invalid = 0
        async with AsyncSessionLocal() as session:
            mirrored, skipped = await mirror_unlinked_legacy_tasks(
                session, limit=self.batch_size
            )
            view_sync = await sync_active_legacy_view_intents(
                session,
                limit=self.batch_size,
            )
            view_sync_scanned = view_sync.scanned
            view_sync_synced = view_sync.synced
            view_sync_cleared = view_sync.cleared
            view_sync_invalid = view_sync.invalid
            reconciled = await LegacyPublicationBridge(session).reconcile_active(
                limit=self.batch_size
            )
            if not self._runtime_backfill_done:
                batch = await PublicationRuntimeProjector(session).backfill_terminal(
                    after_publication_id=self._runtime_backfill_cursor,
                    limit=self.batch_size,
                )
                runtime_scanned = batch.scanned
                runtime_updated = batch.updated
                self._runtime_backfill_cursor = batch.next_cursor
                self._runtime_backfill_done = batch.done
            if not self._views_backfill_done:
                views_batch = await PublicationAutodeleteViewsBackfillService(
                    session
                ).backfill_published(
                    after_publication_id=self._views_backfill_cursor,
                    limit=self.batch_size,
                )
                views_backfill_scanned = views_batch.scanned
                views_backfill_synced = views_batch.synced
                views_backfill_cleared = views_batch.cleared
                views_backfill_invalid = views_batch.invalid
                self._views_backfill_cursor = views_batch.next_cursor
                self._views_backfill_done = views_batch.done

        if (
            mirrored
            or skipped
            or reconciled
            or runtime_scanned
            or runtime_updated
            or view_sync_scanned
            or view_sync_synced
            or view_sync_cleared
            or view_sync_invalid
            or views_backfill_scanned
            or views_backfill_synced
            or views_backfill_cleared
            or views_backfill_invalid
        ):
            logger.debug(
                "Publication reconciler: mirrored={} skipped={} reconciled={} "
                "runtime_scanned={} runtime_updated={} runtime_done={} "
                "view_sync_scanned={} view_sync_synced={} view_sync_cleared={} "
                "view_sync_invalid={} views_backfill_scanned={} "
                "views_backfill_synced={} views_backfill_cleared={} "
                "views_backfill_invalid={} views_backfill_done={}",
                mirrored,
                skipped,
                reconciled,
                runtime_scanned,
                runtime_updated,
                self._runtime_backfill_done,
                view_sync_scanned,
                view_sync_synced,
                view_sync_cleared,
                view_sync_invalid,
                views_backfill_scanned,
                views_backfill_synced,
                views_backfill_cleared,
                views_backfill_invalid,
                self._views_backfill_done,
            )
