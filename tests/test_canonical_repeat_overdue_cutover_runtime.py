from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
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
from app.services.canonical_repeat_recovery_verifier import (
    CanonicalRepeatRecoveryVerifier,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers import canonical_recovery_scheduler as recovery_scheduler_module
from app.workers.canonical_recovery_scheduler import (
    SAFE_OVERDUE_RECOVERY_CUTOVER_ERROR,
    CanonicalRepeatRecoveryCutoverError,
    Scheduler,
)


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


async def _seed_queued_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=109000 + seed,
            username=f"repeat-overdue-cutover-{seed}",
            full_name=f"Repeat Overdue Cutover {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(109100 + seed),
            title=f"Repeat Overdue Cutover {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Overdue cutover"}]
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
            },
        )
        return int(publication.id), int(publication.legacy_post_task_id or 0)


async def _children(session, *, root_task_id: int) -> list[PostTask]:
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


def test_overdue_recovery_cutover_flag_is_default_off_and_requires_shadow() -> None:
    default = _settings()
    assert default.canonical_repeat_overdue_recovery_planning_enabled is False

    enabled = _settings(CANONICAL_REPEAT_OVERDUE_RECOVERY_PLANNING_ENABLED=True)
    assert enabled.canonical_repeat_overdue_recovery_planning_enabled is True
    with pytest.raises(
        RuntimeConfigurationError,
        match=(
            "CANONICAL_REPEAT_OVERDUE_RECOVERY_PLANNING_ENABLED requires "
            "CANONICAL_REPEAT_OVERDUE_RECOVERY_SHADOW_ENABLED"
        ),
    ):
        validate_runtime_configuration(enabled)

    guarded = _settings(
        CANONICAL_REPEAT_OVERDUE_RECOVERY_SHADOW_ENABLED=True,
        CANONICAL_REPEAT_OVERDUE_RECOVERY_PLANNING_ENABLED=True,
    )
    validate_runtime_configuration(guarded)


def test_cutover_materializes_one_canonical_transport_without_legacy_fallback(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-overdue-cutover.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_overdue_recovery_shadow=True,
                    repeat_overdue_recovery_planning=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001 - recovery boundary

                skipped = await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )

                assert skipped is True
                children = await _children(session, root_task_id=task_id)
                assert len(children) == 1
                assert children[0].dedupe_key is not None
                assert children[0].dedupe_key.startswith("canonical-repeat-recovery:")
                assert all(child.dedupe_key is not None for child in children)

                source_task = await session.get(PostTask, task_id)
                assert source_task is not None
                assert source_task.status == "skipped"
                assert source_task.error == "overdue at boot"
                source = await session.get(Publication, publication_id)
                assert source is not None and source.status == "skipped"

                verification = await CanonicalRepeatRecoveryVerifier(session).verify(
                    publication_id
                )
                assert verification.outcome == "matched"
                assert verification.successor_legacy_post_task_id == int(children[0].id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cutover_conflict_raises_safe_error_without_legacy_child(monkeypatch, tmp_path) -> None:
    class ConflictAdapter:
        def __init__(self, _session) -> None:
            pass

        async def materialize(self, source_publication_id: int):
            return SimpleNamespace(outcome="conflict")

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-overdue-cutover-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            _publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )
            monkeypatch.setattr(
                recovery_scheduler_module,
                "CanonicalRepeatRecoveryTransportAdapter",
                ConflictAdapter,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_overdue_recovery_shadow=True,
                    repeat_overdue_recovery_planning=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001

                with pytest.raises(
                    CanonicalRepeatRecoveryCutoverError,
                    match=SAFE_OVERDUE_RECOVERY_CUTOVER_ERROR,
                ):
                    await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
                        session,
                        task,
                        dict(task.payload or {}),
                    )

                assert await _children(session, root_task_id=task_id) == []
                source_task = await session.get(PostTask, task_id)
                assert source_task is not None and source_task.status == "pending"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_commit_verifier_mismatch_still_blocks_stale_delivery(monkeypatch, tmp_path) -> None:
    class MismatchVerifier:
        def __init__(self, _session) -> None:
            pass

        async def verify(self, source_publication_id: int):
            return SimpleNamespace(
                outcome="conflict",
                successor_publication_id=None,
            )

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-overdue-cutover-post-commit.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=3,
                scheduled_at=source_at,
            )
            monkeypatch.setattr(
                recovery_scheduler_module,
                "CanonicalRepeatRecoveryVerifier",
                MismatchVerifier,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_overdue_recovery_shadow=True,
                    repeat_overdue_recovery_planning=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001

                handled = await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )

                assert handled is True
                children = await _children(session, root_task_id=task_id)
                assert len(children) == 1
                assert children[0].dedupe_key is not None
                source_task = await session.get(PostTask, task_id)
                assert source_task is not None and source_task.status == "skipped"
                source = await session.get(Publication, publication_id)
                assert source is not None and source.status == "skipped"
        finally:
            await engine.dispose()

    asyncio.run(run())
