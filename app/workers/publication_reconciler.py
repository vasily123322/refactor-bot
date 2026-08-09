from __future__ import annotations

from loguru import logger

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.legacy_content_mirror import mirror_unlinked_legacy_tasks
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import PublicationRuntimeProjector


class PublicationReconcilerWorker:
    """Keep the new content/publication domain synchronized during migration."""

    def __init__(self, *, interval_seconds: int = 5, batch_size: int = 100):
        self.batch_size = max(1, min(int(batch_size), 500))
        self._runtime_backfill_cursor = 0
        self._runtime_backfill_done = False
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
        async with AsyncSessionLocal() as session:
            mirrored, skipped = await mirror_unlinked_legacy_tasks(
                session, limit=self.batch_size
            )
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

        if mirrored or skipped or reconciled or runtime_scanned or runtime_updated:
            logger.debug(
                "Publication reconciler: mirrored={} skipped={} reconciled={} runtime_scanned={} runtime_updated={} runtime_done={}",
                mirrored,
                skipped,
                reconciled,
                runtime_scanned,
                runtime_updated,
                self._runtime_backfill_done,
            )
