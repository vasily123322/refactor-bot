from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.canonical_repeat_transport_adapter import (
    CanonicalRepeatTransportAdapter,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_published_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=101000 + seed,
            username=f"repeat-adapter-{seed}",
            full_name=f"Repeat Adapter {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(101100 + seed),
            title=f"Repeat Adapter {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Adapter repeat"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_seconds": 3600,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            },
        )
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [101200 + seed],
            "result_link": f"https://t.me/c/{101000 + seed}/{101200 + seed}",
        }
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        assert publication.status == "published"
        return int(publication.id), task_id


async def _counts(session) -> tuple[int, int, int]:
    publications = int((await session.execute(select(func.count(Publication.id)))).scalar_one())
    schedules = int((await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one())
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    return publications, schedules, tasks


async def _reserve(session, *, publication_id: int, source_at: datetime) -> None:
    result = await CanonicalRepeatPlanReservationService(session).reserve_next(
        publication_id,
        after=source_at + timedelta(minutes=10),
    )
    assert result.outcome == "reserved"


def test_adapter_materializes_reserved_successor_after_root_transport_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-adapter-created.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, root_task_id = await _seed_published_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(
                    session,
                    publication_id=publication_id,
                    source_at=source_at,
                )
                source = await session.get(Publication, publication_id)
                root_task = await session.get(PostTask, root_task_id)
                assert source is not None and root_task is not None
                source_content_item_id = int(source.content_item_id)
                source_content_revision = int(source.content_revision)
                source.legacy_post_task_id = None
                await session.delete(root_task)
                await session.commit()
                before = await _counts(session)

                created = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                after_created = await _counts(session)
                existing = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                after_existing = await _counts(session)

                assert created.outcome == "created"
                assert created.publication_id is not None
                assert created.schedule_entry_id is not None
                assert created.legacy_post_task_id is not None
                assert after_created == (
                    before[0] + 1,
                    before[1] + 1,
                    before[2] + 1,
                )
                assert existing.outcome == "existing"
                assert existing.publication_id == created.publication_id
                assert existing.legacy_post_task_id == created.legacy_post_task_id
                assert after_existing == after_created

                child = await session.get(Publication, int(created.publication_id))
                child_schedule = await session.get(
                    ScheduleEntry,
                    int(created.schedule_entry_id),
                )
                child_task = await session.get(PostTask, int(created.legacy_post_task_id))
                assert child is not None and child_schedule is not None and child_task is not None
                expected_at = source_at + timedelta(hours=1)
                expected_options = {
                    "autodelete_seconds": 3600,
                    "autodelete_report": True,
                    "nested": {"mode": "stable"},
                }
                assert child.content_item_id == source_content_item_id
                assert child.content_revision == source_content_revision
                assert child.status == "queued"
                assert child_schedule.status == "pending"
                assert child_schedule.scheduled_at == expected_at.replace(
                    tzinfo=None
                ) or child_schedule.scheduled_at == expected_at
                assert child.meta["repeat_group_id"] == root_task_id
                assert child_schedule.meta["repeat_group_id"] == root_task_id
                assert child.meta["runtime_options"] == expected_options
                assert child_schedule.meta["runtime_options"] == expected_options
                assert child.meta["canonical_repeat_transport_adapter"] is True
                assert child_schedule.meta["canonical_repeat_transport_adapter"] is True
                assert child_task.status == "pending"
                assert child_task.payload["repeat_on"] is True
                assert child_task.payload["repeat_seconds"] == 3600
                assert child_task.payload["repeat_group_id"] == root_task_id
                assert child_task.payload["autodelete_seconds"] == 3600
                assert child_task.payload["autodelete_report"] is True
                assert child_task.payload["nested"] == {"mode": "stable"}
                assert child_task.payload["autodelete_at"] == (
                    expected_at + timedelta(hours=1)
                ).isoformat()
                assert child_task.dedupe_key == (
                    f"canonical-repeat:{publication_id}:{expected_at.isoformat()}"
                )

                verification = await CanonicalRepeatReservationVerifier(session).verify(
                    publication_id
                )
                assert verification.outcome == "matched"
                assert verification.successor_publication_id == int(child.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_adapter_refuses_duplicate_when_unmirrored_legacy_transport_already_exists(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-adapter-existing-transport.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, root_task_id = await _seed_published_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(
                    session,
                    publication_id=publication_id,
                    source_at=source_at,
                )
                existing_transport = PostTask(
                    channel_id=(
                        int((await session.get(Publication, publication_id)).channel_id)  # type: ignore[union-attr]
                    ),
                    status="pending",
                    scheduled_at=source_at + timedelta(hours=1),
                    payload={
                        "type": "text",
                        "text": "Existing transport",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "repeat_group_id": root_task_id,
                    },
                )
                session.add(existing_transport)
                await session.commit()
                await session.refresh(existing_transport)
                before = await _counts(session)

                result = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                after = await _counts(session)

                assert result.outcome == "existing_transport"
                assert result.legacy_post_task_id == int(existing_transport.id)
                assert before == after
                linked = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(existing_transport.id)
                        )
                    )
                ).scalar_one_or_none()
                assert linked is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_adapter_fails_closed_if_source_runtime_intent_changes_after_reservation(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-adapter-source-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed_published_repeat(
                Session,
                seed=3,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(
                    session,
                    publication_id=publication_id,
                    source_at=source_at,
                )
                source = await session.get(Publication, publication_id)
                assert source is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(source.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"autodelete_seconds": 999},
                }
                await session.commit()
                before = await _counts(session)

                result = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                after = await _counts(session)

                assert result.outcome == "conflict"
                assert before == after
        finally:
            await engine.dispose()

    asyncio.run(run())
