from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.domain.sources.models import SourceConnector
from app.services.source_ingestion import SourceIngestionError, SourceIngestionService
from app.services.source_ingestion_lease import SourceIngestionLeaseService
from app.services.source_worker_health import SourceWorkerTickStats, source_worker_health
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


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _source_priority_key(connector: SourceConnector) -> tuple[int, int, datetime]:
    """Prioritize urgent work while stable ties keep rotating-window order."""
    backlog_rank = 0 if telegram_backlog_hint(connector) else 1
    success = _utc_datetime(connector.last_success_at)
    never_succeeded_rank = 0 if success is None else 1
    oldest_success = success or datetime.min.replace(tzinfo=timezone.utc)
    return backlog_rank, never_succeeded_rank, oldest_success


class SourceIngestionWorker:
    """Continuously project enabled source connectors into normalized documents.

    Work is bounded by a rotating connector window, per-connector timeout, durable
    backoff and a DB-backed ingestion lease shared with Studio manual ingestion.
    Within each rotating window, Telegram backlog and the stalest sources run first.
    Exact priority ties retain the raw rotating order so wrap-around fairness is
    unchanged. Each completed tick also emits one low-cardinality health summary.
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
        source_worker_health.set_running(True)
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
            source_worker_health.set_running(False)
            return
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        finally:
            self._task = None
            source_worker_health.set_running(False)

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Sources v2 ingestion iteration failed")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break
        finally:
            source_worker_health.set_running(False)

    async def _rotating_connector_ids(self, session: AsyncSession) -> list[int]:
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

    async def _next_connector_ids(
        self,
        session: AsyncSession,
    ) -> tuple[list[int], int, int, int]:
        raw_ids = await self._rotating_connector_ids(session)
        if not raw_ids:
            return [], 0, 0, 0
        boundary = int(raw_ids[-1])
        rows = list(
            (
                await session.execute(
                    select(SourceConnector).where(SourceConnector.id.in_(raw_ids))
                )
            ).scalars().all()
        )
        rows_by_id = {int(row.id): row for row in rows}
        ordered_rows = [
            rows_by_id[connector_id]
            for connector_id in raw_ids
            if connector_id in rows_by_id
        ]
        eligible = [
            row for row in ordered_rows if not source_worker_backoff_active(row)
        ]
        eligible.sort(key=_source_priority_key)
        return (
            [int(row.id) for row in eligible],
            boundary,
            len(raw_ids),
            len(ordered_rows) - len(eligible),
        )

    @staticmethod
    async def _record_failure(
        session: AsyncSession,
        connector: SourceConnector,
        *,
        connector_id: int,
        failure_kind: str,
    ) -> None:
        await session.rollback()
        await session.refresh(connector, attribute_names=["config"])
        retry_after = mark_source_worker_failure(
            connector,
            failure_kind=failure_kind,
        )
        await session.commit()
        logger.warning(
            "Sources v2 connector={} worker failure kind={} count={} retry_after={}",
            int(connector_id),
            failure_kind,
            source_worker_failure_count(connector),
            retry_after.isoformat(),
        )

    async def _release_lease(self, lease, *, connector_id: int) -> None:
        try:
            async with self.session_factory() as lease_session:
                await SourceIngestionLeaseService(lease_session).release(lease)
        except Exception as exc:
            logger.warning(
                "Sources v2 connector={} lease release failed type={}",
                int(connector_id),
                type(exc).__name__,
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

    @staticmethod
    def _record_tick(
        *,
        started_at: datetime,
        window_selected: int,
        scheduled: int,
        processed: int,
        skipped_backoff: int,
        skipped_busy: int,
        lease_errors: int,
        failures: int,
        timeouts: int,
        ingestion_errors: int,
        unexpected_errors: int,
        new_documents: int,
        candidates_created: int,
        backlog_remaining: int,
        stopped_early: bool,
    ) -> None:
        stats = SourceWorkerTickStats(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            window_selected=window_selected,
            scheduled=scheduled,
            processed=processed,
            skipped_backoff=skipped_backoff,
            skipped_busy=skipped_busy,
            lease_errors=lease_errors,
            failures=failures,
            timeouts=timeouts,
            ingestion_errors=ingestion_errors,
            unexpected_errors=unexpected_errors,
            new_documents=new_documents,
            candidates_created=candidates_created,
            backlog_remaining=backlog_remaining,
            stopped_early=stopped_early,
        )
        source_worker_health.record(stats)
        logger.info(
            "Sources v2 tick selected={} scheduled={} processed={} backoff={} busy={} failures={} timeouts={} new_documents={} candidates={} backlog_remaining={} duration_ms={}",
            stats.window_selected,
            stats.scheduled,
            stats.processed,
            stats.skipped_backoff,
            stats.skipped_busy,
            stats.failures,
            stats.timeouts,
            stats.new_documents,
            stats.candidates_created,
            stats.backlog_remaining,
            stats.duration_ms,
        )

    async def run_once(self) -> int:
        started_at = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            (
                connector_ids,
                rotation_boundary,
                window_selected,
                skipped_backoff,
            ) = await self._next_connector_ids(session)

        if rotation_boundary == 0:
            self._last_connector_id = 0
            self._record_tick(
                started_at=started_at,
                window_selected=0,
                scheduled=0,
                processed=0,
                skipped_backoff=0,
                skipped_busy=0,
                lease_errors=0,
                failures=0,
                timeouts=0,
                ingestion_errors=0,
                unexpected_errors=0,
                new_documents=0,
                candidates_created=0,
                backlog_remaining=0,
                stopped_early=False,
            )
            return 0

        self._last_connector_id = rotation_boundary
        processed = 0
        skipped_busy = 0
        lease_errors = 0
        failures = 0
        timeouts = 0
        ingestion_errors = 0
        unexpected_errors = 0
        new_documents = 0
        candidates_created = 0
        backlog_remaining = 0
        stopped_early = False

        for connector_id in connector_ids:
            if self._stop.is_set():
                stopped_early = True
                break
            current_connector_id = int(connector_id)
            async with self.session_factory() as session:
                connector = await session.get(SourceConnector, current_connector_id)
                if connector is None or not connector.enabled:
                    continue
                if source_worker_backoff_active(connector):
                    skipped_backoff += 1
                    logger.trace(
                        "Sources v2 connector={} skipped by worker backoff",
                        current_connector_id,
                    )
                    continue

                lease_service = SourceIngestionLeaseService(session)
                try:
                    lease = await lease_service.acquire(
                        connector_id=current_connector_id,
                        holder="worker",
                    )
                except Exception as exc:
                    await session.rollback()
                    lease_errors += 1
                    logger.warning(
                        "Sources v2 connector={} lease acquisition failed type={}",
                        current_connector_id,
                        type(exc).__name__,
                    )
                    continue
                if lease is None:
                    skipped_busy += 1
                    logger.trace(
                        "Sources v2 connector={} skipped because ingestion lease is busy",
                        current_connector_id,
                    )
                    continue

                try:
                    try:
                        kind, result = await self._ingest_connector(session, connector)
                        processed += 1
                        new_documents += int(result.documents_created or 0)
                        candidates_created += int(result.candidates_created or 0)
                        if result.documents_created:
                            logger.info(
                                "Sources v2 connector={} kind={} new_documents={} candidates={}",
                                current_connector_id,
                                kind,
                                result.documents_created,
                                result.candidates_created,
                            )
                        if kind == "telegram" and telegram_backlog_hint(connector):
                            backlog_remaining += 1
                            logger.info(
                                "Sources v2 connector={} Telegram backlog may remain cursor={}",
                                current_connector_id,
                                telegram_cursor_message_id(connector),
                            )
                    except asyncio.TimeoutError:
                        failures += 1
                        timeouts += 1
                        await self._record_failure(
                            session,
                            connector,
                            connector_id=current_connector_id,
                            failure_kind="timeout",
                        )
                    except SourceIngestionError:
                        failures += 1
                        ingestion_errors += 1
                        await self._record_failure(
                            session,
                            connector,
                            connector_id=current_connector_id,
                            failure_kind="ingestion_error",
                        )
                    except Exception as exc:
                        failures += 1
                        unexpected_errors += 1
                        await self._record_failure(
                            session,
                            connector,
                            connector_id=current_connector_id,
                            failure_kind="unexpected",
                        )
                        logger.warning(
                            "Sources v2 connector={} unexpected ingestion failure type={}",
                            current_connector_id,
                            type(exc).__name__,
                        )
                finally:
                    await self._release_lease(
                        lease,
                        connector_id=current_connector_id,
                    )

        self._record_tick(
            started_at=started_at,
            window_selected=window_selected,
            scheduled=len(connector_ids),
            processed=processed,
            skipped_backoff=skipped_backoff,
            skipped_busy=skipped_busy,
            lease_errors=lease_errors,
            failures=failures,
            timeouts=timeouts,
            ingestion_errors=ingestion_errors,
            unexpected_errors=unexpected_errors,
            new_documents=new_documents,
            candidates_created=candidates_created,
            backlog_remaining=backlog_remaining,
            stopped_early=stopped_early,
        )
        return processed
