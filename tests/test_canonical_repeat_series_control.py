from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_continuation_authority import (
    CanonicalRepeatContinuationAuthorityService,
)
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
)
from app.services.canonical_repeat_series_control import (
    CanonicalRepeatSeriesControlService,
)
from app.services.canonical_repeat_transport_adapter import (
    CanonicalRepeatTransportAdapter,
)


async def _seed(Session, *, group_id: int = 88001):
    now = datetime(2026, 8, 13, 11, 15, tzinfo=timezone.utc)
    async with Session() as session:
        owner = Client(
            tg_user_id=8800001,
            username="repeat-stop-owner",
            full_name="Repeat Stop Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100880001,
            title="Repeat Stop",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat stop"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )

        source_schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=1,
            channel_id=int(channel.id),
            scheduled_at=now - timedelta(minutes=2),
            timezone="UTC",
            status="completed",
            repeat_rule={"enabled": True, "seconds": 60},
            meta={"repeat_group_id": group_id, "runtime_options": {}},
        )
        session.add(source_schedule)
        await session.flush()
        source = Publication(
            schedule_entry_id=int(source_schedule.id),
            content_item_id=int(item.id),
            content_revision=1,
            channel_id=int(channel.id),
            status="published",
            legacy_post_task_id=None,
            telegram_message_ids=[101],
            attempt_count=1,
            meta={"repeat_group_id": group_id, "runtime_options": {}},
        )
        session.add(source)
        await session.flush()
        session.add(
            PublicationAttempt(
                publication_id=int(source.id),
                attempt=1,
                status="published",
                telegram_message_ids=[101],
                error=None,
                meta={"canonical_delivery": True},
                started_at=now - timedelta(minutes=2),
                finished_at=now - timedelta(minutes=1),
            )
        )

        successor_at = now + timedelta(minutes=1)
        reservation = {
            "version": 1,
            "source_publication_id": int(source.id),
            "source_schedule_entry_id": int(source_schedule.id),
            "repeat_group_id": group_id,
            "channel_id": int(channel.id),
            "content_item_id": int(item.id),
            "content_revision": 1,
            "repeat_seconds": 60,
            "scheduled_at": successor_at.isoformat(),
            "runtime_options": {},
        }
        source.meta = {
            **dict(source.meta or {}),
            CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY: dict(reservation),
        }
        source_schedule.meta = {
            **dict(source_schedule.meta or {}),
            CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY: dict(reservation),
        }

        successor_schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=1,
            channel_id=int(channel.id),
            scheduled_at=successor_at,
            timezone="UTC",
            status="pending",
            repeat_rule={"enabled": True, "seconds": 60},
            meta={"repeat_group_id": group_id, "runtime_options": {}},
        )
        session.add(successor_schedule)
        await session.flush()
        task = PostTask(
            channel_id=int(channel.id),
            status="pending",
            scheduled_at=successor_at,
            payload={
                "type": "text",
                "text": "repeat stop",
                "repeat_on": True,
                "repeat_seconds": 60,
                "repeat_group_id": group_id,
            },
        )
        session.add(task)
        await session.flush()
        successor = Publication(
            schedule_entry_id=int(successor_schedule.id),
            content_item_id=int(item.id),
            content_revision=1,
            channel_id=int(channel.id),
            status="queued",
            legacy_post_task_id=int(task.id),
            attempt_count=0,
            meta={"repeat_group_id": group_id, "runtime_options": {}},
        )
        session.add(successor)
        await session.commit()
        return {
            "user_id": int(owner.tg_user_id),
            "group_id": group_id,
            "source_id": int(source.id),
            "source_schedule_id": int(source_schedule.id),
            "successor_schedule_id": int(successor_schedule.id),
            "task_id": int(task.id),
        }


def test_repeat_series_stop_disables_canonical_and_compatibility_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-series-stop.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session)

            async with Session() as session:
                result = await CanonicalRepeatSeriesControlService(
                    session
                ).stop_owned_series(
                    repeat_group_id=seeded["group_id"],
                    tg_user_id=seeded["user_id"],
                    at=datetime(2026, 8, 13, 11, 16, tzinfo=timezone.utc),
                )
                assert result.outcome == "stopped"
                assert result.canonical_schedules_disabled == 2
                assert result.pending_transports_disabled == 1

            async with Session() as session:
                source_schedule = await session.get(
                    ScheduleEntry, seeded["source_schedule_id"]
                )
                successor_schedule = await session.get(
                    ScheduleEntry, seeded["successor_schedule_id"]
                )
                task = await session.get(PostTask, seeded["task_id"])
                assert source_schedule is not None
                assert successor_schedule is not None
                assert task is not None and task.status == "pending"
                assert source_schedule.repeat_rule["enabled"] is False
                assert successor_schedule.repeat_rule["enabled"] is False
                assert task.payload["repeat_on"] is False
                assert "repeat_seconds" not in task.payload
                assert dict(source_schedule.meta or {})["canonical_repeat_control"][
                    "disabled"
                ] is True

                authority = await CanonicalRepeatContinuationAuthorityService(
                    session
                ).lock_and_prove(seeded["source_id"])
                assert authority is None
                await session.rollback()

            # A reservation committed before the stop cannot be materialized afterward:
            # verifier re-proves source authority and the disabled repeat rule fails closed.
            async with Session() as session:
                materialized = await CanonicalRepeatTransportAdapter(session).materialize(
                    seeded["source_id"]
                )
                assert materialized.outcome in {"ineligible", "conflict"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_series_stop_rejects_foreign_owner_without_mutation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-series-owner.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, group_id=88002)

            async with Session() as session:
                result = await CanonicalRepeatSeriesControlService(
                    session
                ).stop_owned_series(
                    repeat_group_id=seeded["group_id"],
                    tg_user_id=seeded["user_id"] + 1,
                )
                assert result.outcome == "not_found"

            async with Session() as session:
                schedule = await session.get(ScheduleEntry, seeded["source_schedule_id"])
                task = await session.get(PostTask, seeded["task_id"])
                assert schedule is not None and schedule.repeat_rule["enabled"] is True
                assert task is not None and task.payload["repeat_on"] is True
                assert task.payload["repeat_seconds"] == 60
        finally:
            await engine.dispose()

    asyncio.run(run())