from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import (
    CanonicalRuntimeSafetyAudit,
    Publication,
    ScheduleEntry,
)
from app.repositories.content import ContentRepo
from app.services.legacy_runtime_drain import LegacyRuntimeDrainService


def test_production_posttask_creators_are_frozen() -> None:
    for relative in (
        "app/services/posting.py",
        "app/services/publication_bridge.py",
        "app/workers/scheduler.py",
        "app/workers/reliable_scheduler.py",
    ):
        source = Path(relative).read_text(encoding="utf-8")
        assert "PostTask(" not in source, relative


def test_terminal_legacy_runtime_is_archived_before_unlink(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'legacy-drain.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                owner = Client(
                    tg_user_id=9_951_513,
                    username="p6-drain",
                    full_name="P6 Drain",
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-1_009_951_513,
                    title="P6 drain",
                    owner_id=int(owner.id),
                )
                session.add(channel)
                await session.commit()

                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "legacy"}]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                task = PostTask(
                    channel_id=int(channel.id),
                    status="failed",
                    payload={
                        "type": "text",
                        "text": "legacy",
                        "result_ids": [777],
                        "repeat_on": False,
                        "autodelete_views": 100,
                    },
                    dedupe_key="p6-terminal",
                    scheduled_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    error="UNKNOWN_DELIVERY_ERROR",
                )
                session.add(task)
                await session.flush()

                schedule = ScheduleEntry(
                    content_item_id=int(item.id),
                    content_revision=int(item.current_revision),
                    channel_id=int(channel.id),
                    scheduled_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    status="completed",
                    repeat_rule={},
                    meta={},
                )
                session.add(schedule)
                await session.flush()
                publication = Publication(
                    schedule_entry_id=int(schedule.id),
                    content_item_id=int(item.id),
                    content_revision=int(item.current_revision),
                    channel_id=int(channel.id),
                    status="failed",
                    execution_mode="intentional_legacy",
                    legacy_post_task_id=int(task.id),
                    last_error="UNKNOWN_DELIVERY_ERROR",
                    meta={},
                )
                session.add(publication)
                session.add(
                    LegacyTimeViewsDeleteAction(
                        post_task_id=int(task.id),
                        chat_id=int(channel.tg_chat_id),
                        message_ids=[777],
                        target_fingerprint="a" * 64,
                        reservation_token="b" * 64,
                        state="unknown",
                    )
                )
                await session.commit()
                task_id = int(task.id)
                publication_id = int(publication.id)

            async with Session() as session:
                result = await LegacyRuntimeDrainService(session).drain_batch()
                assert result.scanned == 1
                assert result.archived == 1
                assert result.unlinked == 1
                assert result.retained_active == 0

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
                assert publication.meta["legacy_post_task_callback_id"] == task_id
                fingerprint = publication.meta["legacy_runtime_audit_fingerprint"]
                audit = (
                    await session.execute(
                        select(CanonicalRuntimeSafetyAudit).where(
                            CanonicalRuntimeSafetyAudit.source_fingerprint
                            == fingerprint
                        )
                    )
                ).scalar_one()
                assert audit.publication_id == publication_id
                assert audit.state == "terminal_no_replay"
                assert audit.evidence["legacy_transport"][
                    "unknown_delivery_no_replay"
                ] is True
                action = audit.evidence["destructive_action"]
                assert action["state"] == "unknown"
                assert action["message_ids"] == [777]
                assert action["automatic_replay_forbidden"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())
