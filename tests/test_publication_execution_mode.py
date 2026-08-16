from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidateSelector,
)
from app.services.canonical_scheduler_admission import (
    CanonicalSchedulerAdmissionKind,
    CanonicalSchedulerAdmissionService,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
    execution_mode_from_legacy_payload,
    execution_mode_from_runtime_options,
)


async def _session_factory(tmp_path, name: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_channel(session, *, seed: int) -> Channel:
    owner = Client(
        tg_user_id=990000 + seed,
        username=f"execution-mode-{seed}",
        full_name=f"Execution Mode {seed}",
        ui_settings={},
    )
    session.add(owner)
    await session.flush()
    channel = Channel(
        tg_chat_id=-(991000 + seed),
        title=f"Execution Mode {seed}",
        owner_id=int(owner.id),
        is_active=True,
    )
    session.add(channel)
    await session.flush()
    return channel


async def _mirror(
    session,
    *,
    channel_id: int,
    payload: dict,
    when: datetime,
) -> tuple[PostTask, Publication]:
    task = PostTask(
        channel_id=int(channel_id),
        status="pending",
        payload=dict(payload),
        dedupe_key=None,
        scheduled_at=when,
    )
    session.add(task)
    await session.flush()
    publication = await mirror_legacy_post_task(session, task, commit=False)
    assert publication is not None
    await session.commit()
    return task, publication


def test_intrinsic_queue_time_classification_has_no_live_readiness_input() -> None:
    assert execution_mode_from_runtime_options({}) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_runtime_options({"pin_on": True}) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_runtime_options(
        {"autodelete_seconds": 60, "autodelete_views": 10}
    ) == INTENTIONAL_LEGACY_EXECUTION_MODE
    assert execution_mode_from_runtime_options(
        {"autodelete_seconds": 60, "autodelete_report": True}
    ) == INTENTIONAL_LEGACY_EXECUTION_MODE
    assert execution_mode_from_runtime_options(
        {"autodelete_views": 10, "autodelete_report": True, "pin_on": True}
    ) == INTENTIONAL_LEGACY_EXECUTION_MODE
    assert execution_mode_from_runtime_options({"autodelete_report": True}) is None

    # Legacy content/transport fields are irrelevant to the ownership decision.
    assert execution_mode_from_legacy_payload(
        {"type": "text", "text": "hello", "silent": False}
    ) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_legacy_payload(
        {
            "type": "text",
            "text": "hello",
            "repeat_on": True,
            "repeat_seconds": 300,
            "autodelete_views": 50,
        }
    ) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_legacy_payload(
        {"type": "text", "text": "hello", "repeat_on": True, "repeat_seconds": 0}
    ) is None


def test_mirror_persists_canonical_and_intentional_legacy_modes(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "execution-mode-mirror.db")
        try:
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            async with Session() as session:
                channel = await _seed_channel(session, seed=1)
                canonical_task, canonical = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={"type": "text", "text": "canonical"},
                    when=now,
                )
                time_views_task, time_views = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={
                        "type": "text",
                        "text": "time views",
                        "autodelete_seconds": 60,
                        "autodelete_views": 10,
                    },
                    when=now + timedelta(minutes=1),
                )
                report_task, report = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={
                        "type": "text",
                        "text": "report",
                        "autodelete_seconds": 60,
                        "autodelete_report": True,
                    },
                    when=now + timedelta(minutes=2),
                )

                assert canonical.execution_mode == CANONICAL_EXECUTION_MODE
                assert time_views.execution_mode == INTENTIONAL_LEGACY_EXECUTION_MODE
                assert report.execution_mode == INTENTIONAL_LEGACY_EXECUTION_MODE
                assert canonical.legacy_post_task_id == int(canonical_task.id)
                assert time_views.legacy_post_task_id == int(time_views_task.id)
                assert report.legacy_post_task_id == int(report_task.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_persisted_mode_controls_legacy_admission_not_rollout_readiness(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "execution-mode-admission.db")
        try:
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            async with Session() as session:
                channel = await _seed_channel(session, seed=2)
                canonical_task, canonical = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={"type": "text", "text": "canonical"},
                    when=now,
                )
                legacy_task, intentional_legacy = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={
                        "type": "text",
                        "text": "legacy",
                        "autodelete_seconds": 60,
                        "autodelete_views": 10,
                    },
                    when=now,
                )

                canonical_admission = await CanonicalSchedulerAdmissionService(
                    session
                ).classify(task_id=int(canonical_task.id))
                assert canonical_admission.kind is (
                    CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED
                )
                assert canonical_admission.legacy_allowed is False
                assert canonical.execution_mode == CANONICAL_EXECUTION_MODE

                legacy_admission = await CanonicalSchedulerAdmissionService(
                    session
                ).classify(task_id=int(legacy_task.id))
                assert legacy_admission.legacy_allowed is True
                assert intentional_legacy.execution_mode == (
                    INTENTIONAL_LEGACY_EXECUTION_MODE
                )

                canonical.execution_mode = None
                await session.commit()
                null_mode = await CanonicalSchedulerAdmissionService(session).classify(
                    task_id=int(canonical_task.id)
                )
                assert null_mode.kind is CanonicalSchedulerAdmissionKind.FAIL_CLOSED
                assert null_mode.legacy_allowed is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_posttask_presence_cannot_flip_mode_and_canonical_selector_uses_it(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "execution-mode-selector.db")
        try:
            due = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
            async with Session() as session:
                channel = await _seed_channel(session, seed=3)
                canonical_task, canonical = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={"type": "text", "text": "canonical no transport"},
                    when=due,
                )
                _legacy_task, intentional_legacy = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={
                        "type": "text",
                        "text": "legacy no candidate",
                        "autodelete_seconds": 60,
                        "autodelete_views": 10,
                    },
                    when=due,
                )
                _null_task, null_mode = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={"type": "text", "text": "historical null"},
                    when=due,
                )
                null_mode.execution_mode = None

                canonical_id = int(canonical.id)
                canonical.legacy_post_task_id = None
                await session.flush()
                await session.delete(canonical_task)
                await session.commit()

                canonical = await session.get(Publication, canonical_id)
                assert canonical is not None
                assert canonical.legacy_post_task_id is None
                assert canonical.execution_mode == CANONICAL_EXECUTION_MODE
                assert intentional_legacy.execution_mode == (
                    INTENTIONAL_LEGACY_EXECUTION_MODE
                )
                assert null_mode.execution_mode is None

                candidates = await CanonicalPublicationDeliveryCandidateSelector(
                    session
                ).due(at=due + timedelta(hours=1))
                assert [candidate.publication_id for candidate in candidates] == [canonical_id]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_execution_mode_database_constraint_rejects_unknown_value(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "execution-mode-check.db")
        try:
            async with Session() as session:
                channel = await _seed_channel(session, seed=4)
                _task, publication = await _mirror(
                    session,
                    channel_id=int(channel.id),
                    payload={"type": "text", "text": "constraint"},
                    when=datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
                )
                publication.execution_mode = "not-an-execution-mode"
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()

                persisted = (
                    await session.execute(
                        select(Publication).where(Publication.id == int(publication.id))
                    )
                ).scalar_one()
                assert persisted.execution_mode == CANONICAL_EXECUTION_MODE
        finally:
            await engine.dispose()

    asyncio.run(run())
