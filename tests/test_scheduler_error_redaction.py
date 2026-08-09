from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo
from app.services.planner import PlannerService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_task_lease import SchedulerTaskLeaseService
from app.workers.publication_scheduler import SAFE_DELIVERY_ERROR, Scheduler


class _FailingPosting:
    def __init__(self, error: BaseException) -> None:
        self.bot = SimpleNamespace()
        self.error = error

    async def send_now(self, *args, **kwargs):
        raise self.error


def test_scheduler_redacts_provider_error_before_logs_and_durable_studio_state(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'scheduler-error-redaction.db'}"
        )
        secret = "bot_token=123456:SUPER-SECRET https://api.example.test/?key=private-key"
        log_output = io.StringIO()
        sink_id = logger.add(log_output, format="{message}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                client = await ClientsRepo(session).create_or_get(
                    91001,
                    "scheduler_test",
                    "Scheduler Test",
                )
                channel = await ChannelsRepo(session).create(
                    int(client.id),
                    -100910010001,
                    "Redaction",
                )
                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Ship safely"}]
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id)
                )
                publication_id = int(publication.id)
                schedule_id = int(publication.schedule_entry_id or 0)
                task_id = int(publication.legacy_post_task_id or 0)
                channel_id = int(channel.id)

            scheduler = Scheduler(
                Session,
                _FailingPosting(RuntimeError(secret)),
                lease_heartbeat_seconds=60,
            )
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                batch = [task]
                await scheduler._mark_processing(session, batch)
                assert len(batch) == 1

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                await scheduler._process_items(session, [task])

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = await session.get(Publication, publication_id)
                lease = await SchedulerTaskLeaseService(session).current(task_id)
                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                planner = await PlannerService(session).get_entry(
                    channel_id=channel_id,
                    schedule_id=schedule_id,
                )

                assert task is not None and task.status == "failed"
                assert task.error == SAFE_DELIVERY_ERROR
                assert publication is not None and publication.status == "failed"
                assert publication.last_error == SAFE_DELIVERY_ERROR
                assert lease is None
                assert len(attempts) == 1
                assert attempts[0].status == "failed"
                assert attempts[0].error == SAFE_DELIVERY_ERROR
                assert planner.publication_status == "failed"
                assert planner.last_error == SAFE_DELIVERY_ERROR

            rendered_logs = log_output.getvalue()
            assert secret not in rendered_logs
            assert "SUPER-SECRET" not in rendered_logs
            assert "private-key" not in rendered_logs
            assert "RuntimeError" in rendered_logs
            assert SAFE_DELIVERY_ERROR in rendered_logs
        finally:
            logger.remove(sink_id)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_transport_redaction_preserves_cancellation() -> None:
    async def run() -> None:
        scheduler = Scheduler(
            lambda: None,
            _FailingPosting(asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            await scheduler.posting.send_now(-1001, {"type": "text", "text": "x"})

    asyncio.run(run())
