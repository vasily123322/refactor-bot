from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_primary_delivery_capability import (
    CanonicalPublicationPrimaryDeliveryCapabilityPlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_transport_retired(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    runtime_options: dict,
    repeat_rule: dict | None = None,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=129000 + seed,
            username=f"canonical-primary-capability-{seed}",
            full_name=f"Canonical Primary Capability {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(129100 + seed),
            title=f"Canonical Primary Capability {seed}",
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
                        "text": "Canonical primary-only delivery proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule=deepcopy(repeat_rule),
            runtime_options=deepcopy(runtime_options),
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_neutral_nonrepeat_runtime_is_primary_delivery_capable_without_transport(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-primary-capable.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            due = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_transport_retired(
                Session,
                seed=1,
                scheduled_at=due,
                runtime_options={
                    "silent": False,
                    "pin_on": False,
                    "forward_to": [],
                    "autodelete_seconds": 0,
                    "autodelete_effective_seconds": 0,
                    "autodelete_views": 0,
                    "autodelete_report": False,
                },
                repeat_rule={"enabled": False},
            )

            async with Session() as session:
                capability = await CanonicalPublicationPrimaryDeliveryCapabilityPlanner(
                    session
                ).plan(
                    publication_id,
                    at=due + timedelta(minutes=1),
                )
                assert capability is not None
                assert capability.delivery.publication_id == publication_id
                assert capability.delivery.runtime_options()["silent"] is False
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_effectful_secondary_runtime_options_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-primary-effectful.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            due = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            options = [
                {"silent": True},
                {"pin_on": True},
                {"forward_to": [999]},
                {"autodelete_seconds": 60},
                {"autodelete_effective_seconds": 60},
                {"autodelete_views": 10},
                {"autodelete_report": True},
            ]
            publication_ids = []
            for index, runtime in enumerate(options, start=10):
                publication_ids.append(
                    await _seed_transport_retired(
                        Session,
                        seed=index,
                        scheduled_at=due,
                        runtime_options=runtime,
                        repeat_rule={"enabled": False},
                    )
                )

            async with Session() as session:
                planner = CanonicalPublicationPrimaryDeliveryCapabilityPlanner(session)
                for publication_id in publication_ids:
                    assert await planner.plan(
                        publication_id,
                        at=due + timedelta(minutes=1),
                    ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_enabled_repeat_is_not_primary_only_even_with_neutral_runtime(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-primary-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            due = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_transport_retired(
                Session,
                seed=30,
                scheduled_at=due,
                runtime_options={},
                repeat_rule={"enabled": True, "seconds": 300},
            )

            async with Session() as session:
                assert await CanonicalPublicationPrimaryDeliveryCapabilityPlanner(
                    session
                ).plan(
                    publication_id,
                    at=due + timedelta(minutes=1),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unknown_runtime_option_fails_closed_instead_of_being_ignored(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-primary-unknown.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            due = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed_transport_retired(
                Session,
                seed=31,
                scheduled_at=due,
                runtime_options={"future_side_effect": True},
                repeat_rule={"enabled": False},
            )

            async with Session() as session:
                assert await CanonicalPublicationPrimaryDeliveryCapabilityPlanner(
                    session
                ).plan(
                    publication_id,
                    at=due + timedelta(minutes=1),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
