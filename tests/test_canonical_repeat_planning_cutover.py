from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.config import Settings
from app.core.db import Base
from app.core.runtime_configuration import (
    RuntimeConfigurationError,
    validate_runtime_configuration,
)
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers import canonical_scheduler as scheduler_module
from app.workers.canonical_scheduler import Scheduler


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


async def _seed_repeat(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=102000 + seed,
            username=f"repeat-cutover-{seed}",
            full_name=f"Repeat Cutover {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(102100 + seed),
            title=f"Repeat Cutover {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Cutover repeat"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        scheduled_at = datetime.now(timezone.utc) + timedelta(hours=2)
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
            },
        )
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [102200 + seed],
            "result_link": f"https://t.me/c/{102000 + seed}/{102200 + seed}",
        }
        await session.commit()
        return int(publication.id), task_id


async def _successors(session, *, root_task_id: int) -> list[PostTask]:
    return list(
        (
            await session.execute(
                select(PostTask)
                .where(
                    PostTask.id != int(root_task_id),
                    PostTask.payload["repeat_group_id"].as_integer()
                    == int(root_task_id),
                )
                .order_by(PostTask.id.asc())
            )
        ).scalars().all()
    )


def test_cutover_flag_is_default_off_and_requires_shadow_guard() -> None:
    default = _settings()
    assert default.canonical_repeat_shadow_planning_enabled is False
    assert default.canonical_repeat_transport_adapter_enabled is False

    unsafe = _settings(
        CANONICAL_REPEAT_TRANSPORT_ADAPTER_ENABLED=True,
        CANONICAL_REPEAT_SHADOW_PLANNING_ENABLED=False,
    )
    with pytest.raises(
        RuntimeConfigurationError,
        match="CANONICAL_REPEAT_SHADOW_PLANNING_ENABLED",
    ):
        validate_runtime_configuration(unsafe)

    safe = _settings(
        CANONICAL_REPEAT_TRANSPORT_ADAPTER_ENABLED=True,
        CANONICAL_REPEAT_SHADOW_PLANNING_ENABLED=True,
    )
    validate_runtime_configuration(safe)


def test_enabled_cutover_creates_exactly_one_adapter_successor_without_legacy_duplicate(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-cutover-enabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_repeat(Session, seed=1)

            async with Session() as session:
                before_tasks = int(
                    (await session.execute(select(func.count(PostTask.id)))).scalar_one()
                )
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_shadow_planning=True,
                    repeat_transport_adapter=True,
                )
                await scheduler._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )

                children = await _successors(session, root_task_id=task_id)
                after_tasks = int(
                    (await session.execute(select(func.count(PostTask.id)))).scalar_one()
                )
                assert len(children) == 1
                assert after_tasks == before_tasks + 1
                child = children[0]
                assert child.dedupe_key is not None
                assert child.dedupe_key.startswith(f"canonical-repeat:{publication_id}:")

                child_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(child.id)
                        )
                    )
                ).scalar_one_or_none()
                assert child_publication is not None
                assert child_publication.meta["canonical_repeat_transport_adapter"] is True

                verification = await CanonicalRepeatReservationVerifier(session).verify(
                    publication_id
                )
                assert verification.outcome == "matched"
                assert verification.successor_legacy_post_task_id == int(child.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cutover_conflict_is_strict_and_never_falls_back_to_legacy_creator(
    monkeypatch,
    tmp_path,
) -> None:
    class ConflictingAdapter:
        def __init__(self, _session) -> None:
            pass

        async def materialize(self, source_publication_id: int):
            return SimpleNamespace(
                outcome="conflict",
                publication_id=None,
                legacy_post_task_id=None,
            )

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-cutover-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed_repeat(Session, seed=2)
            monkeypatch.setattr(
                scheduler_module,
                "CanonicalRepeatTransportAdapter",
                ConflictingAdapter,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_shadow_planning=True,
                    repeat_transport_adapter=True,
                )
                await scheduler._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )
                assert await _successors(session, root_task_id=task_id) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_disabled_cutover_preserves_legacy_creator(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-cutover-disabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_repeat(Session, seed=3)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_shadow_planning=False,
                    repeat_transport_adapter=False,
                )
                await scheduler._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )
                children = await _successors(session, root_task_id=task_id)
                assert len(children) == 1
                child = children[0]
                assert child.dedupe_key is None
                child_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(child.id)
                        )
                    )
                ).scalar_one_or_none()
                assert child_publication is not None
                assert "canonical_repeat_transport_adapter" not in dict(
                    child_publication.meta or {}
                )
                assert publication_id > 0
        finally:
            await engine.dispose()

    asyncio.run(run())
