from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_autodelete_runtime_planner import (
    CanonicalPublicationAutodeleteRuntimePlanner,
)
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimService,
)
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_delivery_handoff_executor import (
    CanonicalPublicationDeliveryHandoffExecutor,
)
from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecution,
)
from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)
from app.services.canonical_publication_delivery_runtime import (
    CanonicalPublicationDeliveryLiveAutodeleteCoordinator,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CUTOVER_META_KEY,
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_linked(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=195000 + seed,
            username=f"linked-timer-{seed}",
            full_name=f"Linked Timer {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100195000 + seed),
            title=f"Linked Timer {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Linked timer {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=deepcopy(runtime_options),
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


async def _assert_linked_pending(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert publication.legacy_post_task_id == task_id
        assert CUTOVER_META_KEY not in dict(publication.meta or {})
        assert task is not None and task.status == "pending"
        assert await session.get(PublicationDeliveryLease, publication_id) is None


def test_linked_timer_requires_available_canonical_delete_executor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-timer-disabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked(
                Session,
                seed=1,
                runtime_options={
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                result = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    publication_id,
                    holder="timer-disabled",
                    ttl_seconds=120,
                    allow_time_autodelete=False,
                )
                assert result.outcome == "ineligible"
            await _assert_linked_pending(Session, publication_id, task_id)

            # The old commit-only handoff API stays timer-ineligible regardless of the
            # new atomic runtime capability, preventing an unsafe alternate cutover path.
            async with Session() as session:
                direct = await CanonicalPublicationLegacyTransportHandoffService(
                    session
                ).retire_for_canonical_delivery(publication_id)
                assert direct.outcome == "ineligible"
            await _assert_linked_pending(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_exact_pristine_linked_timer_atomically_claims_when_executor_available(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-timer-atomic.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked(
                Session,
                seed=2,
                runtime_options={
                    "silent": True,
                    "autodelete_seconds": 90,
                    "autodelete_views": 0,
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                result = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    publication_id,
                    holder="timer-enabled",
                    ttl_seconds=120,
                    allow_time_autodelete=True,
                )
                assert result.outcome == "claimed"
                assert result.claim is not None
                assert result.claim.plan.runtime_options() == {
                    "silent": True,
                    "autodelete_seconds": 90,
                    "autodelete_views": 0,
                    "autodelete_report": True,
                }

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, task_id) is None
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
                marker = dict(publication.meta or {})[CUTOVER_META_KEY]
                assert marker["atomic_claim"] is True
                assert marker["time_autodelete"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_generated_or_drifted_legacy_timer_state_blocks_atomic_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-timer-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            mutations = [
                (3, lambda p: p.__setitem__("autodelete_seconds", 61)),
                (4, lambda p: p.__setitem__("autodelete_report", False)),
                (5, lambda p: p.__setitem__("autodelete_effective_seconds", 60)),
                (
                    6,
                    lambda p: p.__setitem__(
                        "autodelete_at",
                        "2026-08-11T18:00:00+00:00",
                    ),
                ),
                (7, lambda p: p.__setitem__("result_ids", [777])),
            ]
            for seed, mutate in mutations:
                publication_id, task_id = await _seed_linked(
                    Session,
                    seed=seed,
                    runtime_options={
                        "autodelete_seconds": 60,
                        "autodelete_report": True,
                    },
                )
                async with Session() as session:
                    task = await session.get(PostTask, task_id)
                    assert task is not None
                    payload = dict(task.payload or {})
                    mutate(payload)
                    task.payload = payload
                    await session.commit()

                async with Session() as session:
                    result = await CanonicalPublicationAtomicHandoffClaimService(
                        session
                    ).claim_linked(
                        publication_id,
                        holder="timer-drift",
                        ttl_seconds=120,
                        allow_time_autodelete=True,
                    )
                    assert result.outcome == "ineligible"
                await _assert_linked_pending(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_timer_widening_does_not_admit_views_pin_or_forward_linked_intent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-timer-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            cases = [
                (8, {"autodelete_seconds": 60, "autodelete_views": 10}),
                (9, {"autodelete_seconds": 60, "pin_on": True}),
                (10, {"autodelete_seconds": 60, "forward_to": [1]}),
            ]
            for seed, options in cases:
                publication_id, task_id = await _seed_linked(
                    Session,
                    seed=seed,
                    runtime_options=options,
                )
                async with Session() as session:
                    result = await CanonicalPublicationAtomicHandoffClaimService(
                        session
                    ).claim_linked(
                        publication_id,
                        holder="scope-proof",
                        ttl_seconds=120,
                        allow_time_autodelete=True,
                    )
                    assert result.outcome == "ineligible"
                await _assert_linked_pending(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [3901]


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


def test_linked_timer_runtime_end_to_end_sends_once_and_materializes_timer(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-timer-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked(
                Session,
                seed=11,
                runtime_options={
                    "autodelete_seconds": 75,
                    "autodelete_report": True,
                },
            )
            sender = _Sender()
            timer = CanonicalPublicationDeliveryLiveAutodeleteCoordinator(
                session_factory=Session,
            )
            hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
                executor=_NoopAuxiliaryExecutor(),
                autodelete_writer=timer,
                session_factory=Session,
            )
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                post_send_hook=hook,
                allow_time_autodelete=True,
                heartbeat_interval_seconds=120,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )

            first = await wrapper.execute(publication_id)
            assert first.outcome == "published"
            assert sender.calls == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, task_id) is None
                runtime = dict(publication.meta or {}).get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict)
                assert runtime["deleted"] is False
                assert runtime["effective_seconds"] == 75
                terminal = await CanonicalPublicationAutodeleteRuntimePlanner(
                    session
                ).plan(publication_id)
                assert terminal is not None and terminal.existing is True

            second = await wrapper.execute(publication_id)
            assert second.outcome == "ineligible"
            assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
