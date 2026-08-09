from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.domain.sources.models import SourceConnector
from app.services.source_ingestion import SourceIngestionError, SourceIngestionService
from app.services.source_worker_policy import (
    clear_source_worker_failure,
    mark_source_worker_failure,
    source_worker_backoff_active,
    source_worker_failure_count,
)
from app.services.telegram_source_ingestion import (
    TelegramSourceIngestionService,
    telegram_backlog_hint,
    telegram_cursor_message_id,
)


_SUPPORTED_KINDS = ("rss", "url", "web", "telegram")


class SourceIngestionWorker:
    """Continuously project enabled source connectors into normalized documents.

    Work is bounded both by a rotating connector window and by a per-connector
    timeout. Repeated failures open a durable exponential backoff circuit stored in
    connector config; manual Studio ingestion remains outside that circuit.
    """

    def __init__(
        self,
        *,
        interval_seconds: int = 60,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        max_connectors_per_tick: int = 50,
        connector_timeout_seconds: float = 60.0,
    ):
        self.interval_seconds = max(15, int(interval_seconds))
        self.session_factory = session_factory
        self.max_connectors_per_tick = max(1, min(int(max_connectors_per_tick), 500))
        self.connector_timeout_seconds = max(
            0.05,
            min(float(connector_timeout_seconds), 600.0),
        )
        self._last_connector_id = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="source-ingestion")
        logger.info(
            "Sources v2 ingestion worker started interval={}s max_connectors_per_tick={} timeout={}s",
            self.interval_seconds,
            self.max_connectors_per_tick,
            self.connector_timeout_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Sources v2 ingestion iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def _next_connector_ids(self, session: AsyncSession) -> list[int]:
        common = (
            SourceConnector.enabled.is_(True),
            SourceConnector.kind.in_(_SUPPORTED_KINDS),
        )
        remaining = self.max_connectors_per_tick
        ids = list(
            (
                await session.execute(
                    select(SourceConnector.id)
                    .where(*common, SourceConnector.id > int(self._last_connector_id))
                    .order_by(SourceConnector.id.asc())
                    .limit(remaining)
                )
            ).scalars().all()
        )
        remaining -= len(ids)
        if remaining > 0 and self._last_connector_id > 0:
            ids.extend(
                list(
                    (
                        await session.execute(
                            select(SourceConnector.id)
                            .where(
                                *common,
                                SourceConnector.id <= int(self._last_connector_id),
                            )
                            .order_by(SourceConnector.id.asc())
                            .limit(remaining)
                        )
                    ).scalars().all()
                )
            )
        return [int(value) for value in ids]

    @staticmethod
    async def _record_failure(
        session: AsyncSession,
        connector: SourceConnector,
        *,
        failure_kind: str,
    ) -> None:
        await session.rollback()
        # Refresh config so an overlapping manual run cannot have its newer state
        # overwritten by a stale worker snapshot.
        await session.refresh(connector, attribute_names=["config"])
        retry_after = mark_source_worker_failure(
            connector,
            failure_kind=failure_kind,
        )
        await session.commit()
        logger.warning(
            "Sources v2 connector={} worker failure kind={} count={} retry_after={}",
            int(connector.id),
            failure_kind,
            source_worker_failure_count(connector),
            retry_after.isoformat(),
        )

    async def _ingest_connector(
        self,
        session: AsyncSession,
        connector: SourceConnector,
    ):
        kind = str(connector.kind).lower()
        service = (
            TelegramSourceIngestionService(session)
            if kind == "telegram"
            else SourceIngestionService(session)
        )
        result = await asyncio.wait_for(
            service.ingest(connector),
            timeout=self.connector_timeout_seconds,
        )
        if clear_source_worker_failure(connector):
            await session.commit()
        return kind, result

    async def run_once(self) -> int:
        async with self.session_factory() as session:
            connector_ids = await self._next_connector_ids(session)

        if not connector_ids:
            self._last_connector_id = 0
            return 0

        self._last_connector_id = int(connector_ids[-1])
        processed = 0
        for connector_id in connector_ids:
            if self._stop.is_set():
                break
            async with self.session_factory() as session:
                connector = await session.get(SourceConnector, int(connector_id))
                if connector is None or not connector.enabled:
                    continue
                if source_worker_backoff_active(connector):
                    logger.trace(
                        "Sources v2 connector={} skipped by worker backoff",
                        int(connector.id),
                    )
                    continue
                try:
                    kind, result = await self._ingest_connector(session, connector)
                    processed += 1
                    if result.documents_created:
                        logger.info(
                            "Sources v2 connector={} kind={} new_documents={} candidates={}",
                            int(connector.id),
                            kind,
                            result.documents_created,
                            result.candidates_created,
                        )
                    if kind == "telegram" and telegram_backlog_hint(connector):
                        logger.info(
                            "Sources v2 connector={} Telegram backlog may remain cursor={}",
                            int(connector.id),
                            telegram_cursor_message_id(connector),
                        )
                except asyncio.TimeoutError:
                    await self._record_failure(
                        session,
                        connector,
                        failure_kind="timeout",
                    )
                except SourceIngestionError:
                    await self._record_failure(
                        session,
                        connector,
                        failure_kind="ingestion_error",
                    )
                except Exception as exc:
                    await self._record_failure(
                        session,
                        connector,
                        failure_kind="unexpected",
                    )
                    logger.warning(
                        "Sources v2 connector={} unexpected ingestion failure type={}",
                        int(connector.id),
                        type(exc).__name__,
                    )
        return processed
