from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.planner import PlannerService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_errors import (
    GENERIC_SCHEDULER_ERROR,
    NO_MESSAGE_IDS_ERROR,
    SAFE_DELIVERY_ERROR,
    UNKNOWN_DELIVERY_ERROR,
    public_scheduler_error,
)


def test_public_scheduler_error_preserves_only_code_controlled_values() -> None:
    for value in (
        SAFE_DELIVERY_ERROR,
        UNKNOWN_DELIVERY_ERROR,
        NO_MESSAGE_IDS_ERROR,
        "telegram unavailable",
    ):
        assert public_scheduler_error(value) == value

    assert public_scheduler_error(None) == GENERIC_SCHEDULER_ERROR
    assert public_scheduler_error("") == GENERIC_SCHEDULER_ERROR
    assert public_scheduler_error(" timeout from provider ") == GENERIC_SCHEDULER_ERROR
    assert (
        public_scheduler_error("postgresql://user:password@db/internal")
        == GENERIC_SCHEDULER_ERROR
    )


def test_reconcile_keeps_raw_legacy_error_out_of_new_domain_and_planner() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            raw_error = (
                "psycopg failure postgresql://user:password@db/internal "
                "bot_token=123456:SUPER-SECRET https://api.example.test/?key=private"
            )
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=701,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Redact old error"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                publication = await bridge.queue(content_item_id=int(item.id))
                publication_id = int(publication.id)
                schedule_id = int(publication.schedule_entry_id or 0)
                task_id = int(publication.legacy_post_task_id or 0)

                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "failed"
                task.error = raw_error
                await session.commit()

                publication = await bridge.reconcile(publication_id)
                assert publication.status == "failed"
                assert publication.last_error == GENERIC_SCHEDULER_ERROR

                attempts = list(
                    (
                        await session.execute(
                            select(PublicationAttempt).where(
                                PublicationAttempt.publication_id == publication_id
                            )
                        )
                    ).scalars().all()
                )
                assert len(attempts) == 1
                assert attempts[0].status == "failed"
                assert attempts[0].error == GENERIC_SCHEDULER_ERROR

                planner = await PlannerService(session).get_entry(
                    channel_id=701,
                    schedule_id=schedule_id,
                )
                assert planner.publication_status == "failed"
                assert planner.last_error == GENERIC_SCHEDULER_ERROR

                # Compatibility storage is intentionally not rewritten by projection.
                task = await session.get(PostTask, task_id)
                assert task is not None
                assert task.error == raw_error

                public_values = [
                    publication.last_error,
                    attempts[0].error,
                    planner.last_error,
                ]
                for value in public_values:
                    assert value == GENERIC_SCHEDULER_ERROR
                    assert "SUPER-SECRET" not in str(value)
                    assert "password" not in str(value)
                    assert "private" not in str(value)
        finally:
            await engine.dispose()

    asyncio.run(run())
