from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.domain.sources.models import SourceConnector
from app.services.source_ingestion import SourceIngestionError, SourceIngestionService
from app.services.telegram_source_ingestion import (
    TelegramSourceIngestionService,
    telegram_backlog_hint,
    telegram_cursor_message_id,
)


_SUPPORTED_KINDS = ("rss", "url", "web", "telegram")


class SourceIngestionWorker:
    """Continuously project enabled source connectors into normalized documents.

    Each tick processes a bounded rotating window rather than every connector in the
    database. The cursor wraps by connector id, so persistent failures or large
    Telegram backlogs cannot starve higher-id sources indefinitely.
    """

    def __init__(
        self,
        *,
        interval_seconds: int = 60,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        max_connectors_per_tick: int = 50,
    ):
        self.interval_seconds = max(15, int(interval_seconds))
        self.session_factory = session_factory
        self.max_connectors_per_tick = max(1, min(int(max_connectors_per_tick), 500))
        self._last_connector_id = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="source-ingestion")
        logger.info(
            "Sources v2 ingestion worker started interval={}s max_connectors_per_tick={}",
            self.interval_seconds,
            self.max_connectors_per_tick,
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

    async def run_once(self) -> int:
        async with self.session_factory() as session:
            connector_ids = await self._next_connector_ids(session)

        if not connector_ids:
            self._last_connector_id = 0
            return 0

        # Rotate even if individual connectors fail. A broken low-id connector must
        # not monopolize the first slot forever.
        self._last_connector_id = int(connector_ids[-1])
        processed = 0
        for connector_id in connector_ids:
            if self._stop.is_set():
                break
            async with self.session_factory() as session:
                connector = await session.get(SourceConnector, int(connector_id))
                if connector is None or not connector.enabled:
                    continue
                try:
                    kind = str(connector.kind).lower()
                    if kind == "telegram":
                        result = await TelegramSourceIngestionService(session).ingest(connector)
                    else:
                        result = await SourceIngestionService(session).ingest(connector)
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
                except SourceIngestionError as exc:
                    logger.warning(
                        "Sources v2 connector={} ingestion failed: {}",
                        int(connector.id),
                        exc,
                    )
                except Exception:
                    logger.exception(
                        "Sources v2 connector={} unexpected ingestion failure",
                        int(connector.id),
                    )
        return processed
