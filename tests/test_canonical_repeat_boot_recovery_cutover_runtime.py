from __future__ import annotations

import asyncio
from copy import deepcopy
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
from app.services.canonical_repeat_boot_recovery_verifier import (
    CanonicalRepeatBootRecoveryVerifier,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import cleanup_runtime_fields
from app.workers import canonical_recovery_scheduler as recovery_scheduler_module
from app.workers.canonical_recovery_scheduler import (
    SAFE_BOOT_RECOVERY_CUTOVER_ERROR,
    CanonicalRepeatBootRecoveryCutoverError,
    Scheduler,
)


_RUNTIME_OPTIONS = {
    "autodelete_seconds": 3600,
    "autodelete_report": True,
}


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


async def _seed_group(
    Session,
    *,
    seed: int,
    source_at: datetime,
) -> tuple[tuple[int, int], tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=116000 + seed,
            username=f"repeat-boot-cutover-{seed}",
            full_name=f"Repeat Boot Cutover {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(116100 + seed),
            title=f"Repeat Boot Cutover {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Boot cutover"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        root_publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=source_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        root_task_id = int(root_publication.legacy_post_task_id or 0)
        root_task = await session.get(PostTask, root_task_id)
        assert root_task is not None

        second_payload = cleanup_runtime_fields(dict(root_task.payload or {}))
        second_payload["repeat_on"] = True
        second_payload["repeat_seconds"] = 3600
        second_payload["repeat_group_id"] = root_task_id
        second_task = PostTask(
            channel_id=int(root_task.channel_id),
            status="pending",
            payload=second_payload,
            dedupe_key=None,
            scheduled_at=source_at + timedelta(hours=1),
        )
        session.add(second_task)
        await session.commit()
        await session.refresh(second_task)
        second_publication = await mirror_legacy_post_task(session, second_task)
        assert second_publication is not None
        return (
            (int(root_publication.id), int(second_publication.id)),
            (root_task_id, int(second_task.id)),
        )


async def _target_transports(
    session,
    *,
    repeat_group_id: int,
    target_at: datetime,
) -> list[PostTask]:
    rows = list(
        (
            await session.execute(
                select(PostTask)
                .where(
                    PostTask.payload["repeat_group_id"].as_integer()
                    == int(repeat_group_id),
                    PostTask.status == "pending",
                )
                .order_by(PostTask.id.asc())
            )
        ).scalars().all()
    )
    expected = target_at.replace(tzinfo=None)
    return [
        task
        for task in rows
        if task.scheduled_at == target_at or task.scheduled_at == expected
    ]


def test_boot_group_cutover_flag_is_default_off_and_requires_shadow() -> None:
    default = _settings()
    assert default.canonical_repeat_boot_recovery_planning_enabled is False

    unsafe = _settings(CANONICAL_REPEAT_BOOT_RECOVERY_PLANNING_ENABLED=True)
    with pytest.raises(
        RuntimeConfigurationError,
        match=(
            "CANONICAL_REPEAT_BOOT_RECOVERY_PLANNING_ENABLED requires "
            "CANONICAL_REPEAT_BOOT_RECOVERY_SHADOW_ENABLED"
        ),
    ):
        validate_runtime_configuration(unsafe)

    guarded = _settings(
        CANONICAL_REPEAT_BOOT_RECOVERY_SHADOW_ENABLED=True,
        CANONICAL_REPEAT_BOOT_RECOVERY_PLANNING_ENABLED=True,
    )
    validate_runtime_configuration(guarded)


def test_cutover_materializes_whole_group_and_preserves_nonrepeat_remaining(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-cutover.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            publication_ids, task_ids = await _seed_group(
                Session,
                seed=1,
                source_at=source_at,
            )

            async with Session() as session:
                source_tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
                assert all(task is not None for task in source_tasks)
                channel_id = int(source_tasks[0].channel_id)  # type: ignore[union-attr]
                nonrepeat = PostTask(
                    channel_id=channel_id,
                    status="pending",
                    payload={"type": "text", "text": "Keep me"},
                    dedupe_key=None,
                    scheduled_at=source_at,
                )
                session.add(nonrepeat)
                await session.commit()
                await session.refresh(nonrepeat)

                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_boot_recovery_shadow=True,
                    repeat_boot_recovery_planning=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001
                selected = [task for task in source_tasks if task is not None] + [nonrepeat]

                remaining = await scheduler._boot_cleanup_repeats(  # noqa: SLF001
                    session,
                    selected,
                )

                assert [int(post.id) for post in remaining] == [int(nonrepeat.id)]
                targets = await _target_transports(
                    session,
                    repeat_group_id=task_ids[0],
                    target_at=target_at,
                )
                assert len(targets) == 1
                child = targets[0]
                assert child.dedupe_key is not None
                assert child.dedupe_key.startswith(
                    f"canonical-repeat-boot-recovery:{task_ids[0]}:"
                )

                for task_id in task_ids:
                    task = await session.get(PostTask, task_id)
                    assert task is not None
                    assert task.status == "skipped"
                    assert task.error == "overdue at boot"

                verification = await CanonicalRepeatBootRecoveryVerifier(session).verify(
                    publication_ids
                )
                assert verification.outcome == "matched"
                assert verification.successor_legacy_post_task_id == int(child.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cutover_adapter_conflict_raises_safe_error_without_legacy_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    class ConflictAdapter:
        def __init__(self, _session) -> None:
            pass

        async def materialize(self, source_publication_ids):
            return SimpleNamespace(outcome="conflict")

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-cutover-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            _publication_ids, task_ids = await _seed_group(
                Session,
                seed=2,
                source_at=source_at,
            )
            monkeypatch.setattr(
                recovery_scheduler_module,
                "CanonicalRepeatBootRecoveryTransportAdapter",
                ConflictAdapter,
            )

            async with Session() as session:
                tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
                assert all(task is not None for task in tasks)
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_boot_recovery_shadow=True,
                    repeat_boot_recovery_planning=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001

                with pytest.raises(
                    CanonicalRepeatBootRecoveryCutoverError,
                    match=SAFE_BOOT_RECOVERY_CUTOVER_ERROR,
                ):
                    await scheduler._boot_cleanup_repeats(  # noqa: SLF001
                        session,
                        [task for task in tasks if task is not None],
                    )

                assert await _target_transports(
                    session,
                    repeat_group_id=task_ids[0],
                    target_at=target_at,
                ) == []
                for task_id in task_ids:
                    task = await session.get(PostTask, task_id)
                    assert task is not None and task.status == "pending"
                assert scheduler._boot_cleanup_done is False  # noqa: SLF001
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_postcommit_verifier_mismatch_keeps_group_skipped_without_legacy_duplicate(
    monkeypatch,
    tmp_path,
) -> None:
    class MismatchVerifier:
        def __init__(self, _session) -> None:
            pass

        async def verify(self, source_publication_ids):
            return SimpleNamespace(
                outcome="conflict",
                successor_publication_id=None,
            )

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-cutover-postcommit.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            _publication_ids, task_ids = await _seed_group(
                Session,
                seed=3,
                source_at=source_at,
            )
            monkeypatch.setattr(
                recovery_scheduler_module,
                "CanonicalRepeatBootRecoveryVerifier",
                MismatchVerifier,
            )

            async with Session() as session:
                tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
                assert all(task is not None for task in tasks)
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_boot_recovery_shadow=True,
                    repeat_boot_recovery_planning=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001

                remaining = await scheduler._boot_cleanup_repeats(  # noqa: SLF001
                    session,
                    [task for task in tasks if task is not None],
                )

                assert remaining == []
                targets = await _target_transports(
                    session,
                    repeat_group_id=task_ids[0],
                    target_at=target_at,
                )
                assert len(targets) == 1
                assert targets[0].dedupe_key is not None
                for task_id in task_ids:
                    task = await session.get(PostTask, task_id)
                    assert task is not None and task.status == "skipped"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_malformed_group_stays_wholly_on_legacy_boot_cleanup(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-cutover-legacy-group.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            publication_ids, task_ids = await _seed_group(
                Session,
                seed=4,
                source_at=source_at,
            )

            async with Session() as session:
                second = await session.get(PostTask, task_ids[1])
                assert second is not None
                second.payload = {
                    **dict(second.payload or {}),
                    "repeat_seconds": 0,
                }
                await session.commit()
                tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
                assert all(task is not None for task in tasks)
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_boot_recovery_shadow=True,
                    repeat_boot_recovery_planning=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001

                remaining = await scheduler._boot_cleanup_repeats(  # noqa: SLF001
                    session,
                    [task for task in tasks if task is not None],
                )

                assert remaining == []
                targets = await _target_transports(
                    session,
                    repeat_group_id=task_ids[0],
                    target_at=target_at,
                )
                assert len(targets) == 1
                assert targets[0].dedupe_key is None
                for publication_id in publication_ids:
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    assert "canonical_repeat_boot_recovery_reservation" not in dict(
                        publication.meta or {}
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())
