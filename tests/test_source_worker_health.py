from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import ClassVar
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.source_ingestion as worker_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import IngestionResult
from app.services.source_ingestion_lease import SourceIngestionLeaseService
from app.services.source_worker_health import SourceWorkerTickStats, source_worker_health
from app.services.source_worker_policy import mark_source_worker_failure
from app.workers.source_ingestion import SourceIngestionWorker


class _SuccessfulIngestionService:
    calls: ClassVar[list[int]] = []

    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        connector_id = int(connector.id)
        type(self).calls.append(connector_id)
        return IngestionResult(connector_id, 2, 2, 2)


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Health",
                "username": f"health_{user_id}",
            },
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _config() -> StudioConfig:
    return StudioConfig(
        enabled=True,
        host="127.0.0.1",
        port=8080,
        public_url=None,
        init_data_max_age_seconds=86400,
        cors_origins=(),
    )


def test_worker_records_low_cardinality_tick_counters(monkeypatch) -> None:
    async def run() -> None:
        source_worker_health.reset_for_tests()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                backoff = await SourcesRepo(session).create_connector(
                    channel_id=101,
                    kind="url",
                    value="https://example.com/backoff",
                )
                mark_source_worker_failure(
                    backoff,
                    failure_kind="timeout",
                    now=datetime.now(timezone.utc),
                )
                busy = await SourcesRepo(session).create_connector(
                    channel_id=101,
                    kind="url",
                    value="https://example.com/busy",
                )
                success = await SourcesRepo(session).create_connector(
                    channel_id=101,
                    kind="url",
                    value="https://example.com/success",
                )
                await session.commit()
                busy_lease = await SourceIngestionLeaseService(session).acquire(
                    connector_id=int(busy.id),
                    holder="studio",
                )
                assert busy_lease is not None
                success_id = int(success.id)

            _SuccessfulIngestionService.calls = []
            monkeypatch.setattr(
                worker_module,
                "SourceIngestionService",
                _SuccessfulIngestionService,
            )
            worker = SourceIngestionWorker(
                session_factory=Session,
                interval_seconds=15,
                max_connectors_per_tick=3,
            )
            assert await worker.run_once() == 1
            assert _SuccessfulIngestionService.calls == [success_id]

            snapshot = source_worker_health.snapshot()
            assert snapshot["ticks"] == 1
            last = snapshot["last_tick"]
            assert last is not None
            assert last["window_selected"] == 3
            assert last["scheduled"] == 2
            assert last["processed"] == 1
            assert last["skipped_backoff"] == 1
            assert last["skipped_busy"] == 1
            assert last["failures"] == 0
            assert last["new_documents"] == 2
            assert last["candidates_created"] == 2
            assert last["duration_ms"] >= 0
            totals = snapshot["totals"]
            assert totals["processed"] == 1
            assert totals["skipped_backoff"] == 1
            assert totals["skipped_busy"] == 1

            async with Session() as session:
                assert await SourceIngestionLeaseService(session).release(busy_lease) is True
        finally:
            await engine.dispose()
            source_worker_health.reset_for_tests()

    asyncio.run(run())


def test_studio_worker_health_endpoint_is_signed_and_aggregate_only() -> None:
    async def run() -> None:
        source_worker_health.reset_for_tests()
        started = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        source_worker_health.set_running(True)
        source_worker_health.record(
            SourceWorkerTickStats(
                started_at=started,
                finished_at=started,
                window_selected=4,
                scheduled=3,
                processed=2,
                skipped_backoff=1,
                skipped_busy=1,
                failures=1,
                timeouts=1,
                new_documents=5,
                candidates_created=4,
            )
        )
        app = create_studio_app(_config())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
            unauthorized = await client.get("/api/studio/source-worker/health")
            assert unauthorized.status_code == 401

            response = await client.get(
                "/api/studio/source-worker/health",
                headers={"X-Telegram-Init-Data": _init_data(9161)},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["running"] is True
            assert body["ticks"] == 1
            assert body["last_tick"]["processed"] == 2
            assert body["totals"]["timeouts"] == 1
            serialized = response.text.lower()
            for forbidden in (
                "connector_id",
                "source_url",
                "lease_token",
                "api_key",
                "https://example.com",
            ):
                assert forbidden not in serialized
        source_worker_health.reset_for_tests()

    asyncio.run(run())
