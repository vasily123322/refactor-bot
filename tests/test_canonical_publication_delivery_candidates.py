from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidateSelector,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_publication(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    runtime_options: dict | None = None,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=120000 + seed,
            username=f"canonical-candidate-{seed}",
            full_name=f"Canonical Candidate {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(120100 + seed),
            title=f"Canonical Candidate {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Candidate {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options=runtime_options or {},
        )
        return (
            int(publication.id),
            int(publication.legacy_post_task_id or 0),
            int(channel.id),
        )


async def _retire_transport(session, publication_id: int, task_id: int) -> None:
    publication = await session.get(Publication, publication_id)
    task = await session.get(PostTask, task_id)
    assert publication is not None and task is not None
    publication.legacy_post_task_id = None
    await session.delete(task)


def test_due_candidates_are_transport_independent_ordered_and_bounded(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-candidates-order.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            third_id, third_task, _ = await _seed_publication(
                Session,
                seed=1,
                scheduled_at=base + timedelta(minutes=3),
            )
            first_id, first_task, _ = await _seed_publication(
                Session,
                seed=2,
                scheduled_at=base + timedelta(minutes=1),
            )
            second_id, second_task, _ = await _seed_publication(
                Session,
                seed=3,
                scheduled_at=base + timedelta(minutes=2),
            )

            async with Session() as session:
                for publication_id, task_id in (
                    (third_id, third_task),
                    (first_id, first_task),
                    (second_id, second_task),
                ):
                    await _retire_transport(session, publication_id, task_id)
                await session.commit()

                remaining_tasks = (
                    await session.execute(select(PostTask.id))
                ).scalars().all()
                assert remaining_tasks == []

                candidates = await CanonicalPublicationDeliveryCandidateSelector(
                    session
                ).due(
                    limit=2,
                    at=base + timedelta(minutes=10),
                )
                assert [item.publication_id for item in candidates] == [
                    first_id,
                    second_id,
                ]
                assert [item.scheduled_at for item in candidates] == [
                    base + timedelta(minutes=1),
                    base + timedelta(minutes=2),
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_selector_skips_future_inactive_and_existing_attempt_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-candidates-filter.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            valid_id, _valid_task, _ = await _seed_publication(
                Session,
                seed=10,
                scheduled_at=base,
            )
            future_id, _future_task, _ = await _seed_publication(
                Session,
                seed=11,
                scheduled_at=base + timedelta(hours=1),
            )
            inactive_id, _inactive_task, inactive_channel_id = await _seed_publication(
                Session,
                seed=12,
                scheduled_at=base - timedelta(minutes=3),
            )
            attempted_id, _attempted_task, _ = await _seed_publication(
                Session,
                seed=13,
                scheduled_at=base - timedelta(minutes=2),
            )

            async with Session() as session:
                channel = await session.get(Channel, inactive_channel_id)
                assert channel is not None
                channel.is_active = False
                session.add(
                    PublicationAttempt(
                        publication_id=attempted_id,
                        attempt=1,
                        status="sending",
                        telegram_message_ids=None,
                        error=None,
                        meta={},
                    )
                )
                await session.commit()

                candidates = await CanonicalPublicationDeliveryCandidateSelector(
                    session
                ).due(limit=10, at=base + timedelta(minutes=1))
                assert [item.publication_id for item in candidates] == [valid_id]
                assert future_id not in {item.publication_id for item in candidates}
                assert inactive_id not in {item.publication_id for item in candidates}
                assert attempted_id not in {item.publication_id for item in candidates}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_planner_remains_authority_when_runtime_intent_drift_passes_coarse_query(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-candidates-runtime.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            drift_id, _drift_task, _ = await _seed_publication(
                Session,
                seed=20,
                scheduled_at=base - timedelta(minutes=2),
                runtime_options={"silent": True},
            )
            valid_id, _valid_task, _ = await _seed_publication(
                Session,
                seed=21,
                scheduled_at=base - timedelta(minutes=1),
                runtime_options={"silent": True},
            )

            async with Session() as session:
                drift = await session.get(Publication, drift_id)
                assert drift is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(drift.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"silent": False},
                }
                await session.commit()

                candidates = await CanonicalPublicationDeliveryCandidateSelector(
                    session
                ).due(limit=1, at=base)
                assert [item.publication_id for item in candidates] == [valid_id]
                assert drift_id not in {item.publication_id for item in candidates}
        finally:
            await engine.dispose()

    asyncio.run(run())
