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
from app.domain.publication_delivery import PublicationDeliveryAction, PublicationDeliveryLease
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
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
from app.services.canonical_publication_delivery_live_post_action_executor import (
    CanonicalPublicationDeliveryLivePostActionExecutor,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CUTOVER_META_KEY,
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_linked(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=196000 + seed,
            username=f"linked-pin-{seed}",
            full_name=f"Linked Pin {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100196000 + seed),
            title=f"Linked Pin {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Linked pin {seed}"}
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
        return (
            int(publication.id),
            int(publication.legacy_post_task_id),
            int(channel.tg_chat_id),
        )


async def _assert_linked_pending(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert publication.legacy_post_task_id == task_id
        assert task is not None and task.status == "pending"
        assert await session.get(PublicationDeliveryLease, publication_id) is None


def test_exact_linked_pin_atomically_claims_without_timer_dependency(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-pin-atomic.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _chat_id = await _seed_linked(
                Session,
                seed=1,
                runtime_options={"silent": True, "pin_on": True},
            )

            async with Session() as session:
                result = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    publication_id,
                    holder="pin-atomic",
                    ttl_seconds=120,
                    allow_time_autodelete=False,
                )
                assert result.outcome == "claimed"
                assert result.claim is not None
                assert result.claim.plan.runtime_options() == {
                    "silent": True,
                    "pin_on": True,
                }

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, task_id) is None
                marker = dict(publication.meta or {})[CUTOVER_META_KEY]
                assert marker["atomic_claim"] is True
                assert marker["pin_on"] is True
                assert marker["time_autodelete"] is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_direct_commit_only_handoff_stays_pin_ineligible(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-pin-direct-blocked.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _chat_id = await _seed_linked(
                Session,
                seed=2,
                runtime_options={"pin_on": True},
            )
            async with Session() as session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    session
                ).retire_for_canonical_delivery(publication_id)
                assert result.outcome == "ineligible"
            await _assert_linked_pending(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pin_payload_drift_or_hidden_forward_blocks_atomic_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-pin-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            mutations = [
                (3, lambda payload: payload.__setitem__("pin_on", False)),
                (4, lambda payload: payload.__setitem__("forward_to", [999999])),
            ]
            for seed, mutate in mutations:
                publication_id, task_id, _chat_id = await _seed_linked(
                    Session,
                    seed=seed,
                    runtime_options={"pin_on": True},
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
                        holder="pin-drift",
                        ttl_seconds=120,
                    )
                    assert result.outcome == "ineligible"
                await _assert_linked_pending(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_timer_plus_pin_requires_timer_executor_but_composes_when_available(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-pin-timer.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            blocked_id, blocked_task, _ = await _seed_linked(
                Session,
                seed=5,
                runtime_options={
                    "pin_on": True,
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                },
            )
            async with Session() as session:
                blocked = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    blocked_id,
                    holder="timer-unavailable",
                    ttl_seconds=120,
                    allow_time_autodelete=False,
                )
                assert blocked.outcome == "ineligible"
            await _assert_linked_pending(Session, blocked_id, blocked_task)

            claimed_id, claimed_task, _ = await _seed_linked(
                Session,
                seed=6,
                runtime_options={
                    "pin_on": True,
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                },
            )
            async with Session() as session:
                claimed = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    claimed_id,
                    holder="timer-available",
                    ttl_seconds=120,
                    allow_time_autodelete=True,
                )
                assert claimed.outcome == "claimed"
            async with Session() as session:
                publication = await session.get(Publication, claimed_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, claimed_task) is None
                marker = dict(publication.meta or {})[CUTOVER_META_KEY]
                assert marker["pin_on"] is True
                assert marker["time_autodelete"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [4101, 4102]


class _PinBot:
    def __init__(self) -> None:
        self.pins: list[dict] = []

    async def pin_chat_message(self, **kwargs) -> None:
        self.pins.append(dict(kwargs))

    async def forward_message(self, **kwargs) -> None:
        raise AssertionError("pin-only handoff must not forward")


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


def test_linked_pin_end_to_end_pins_last_primary_message_once(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-pin-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, source_chat_id = await _seed_linked(
                Session,
                seed=7,
                runtime_options={"pin_on": True},
            )
            sender = _Sender()
            bot = _PinBot()
            post_actions = CanonicalPublicationDeliveryLivePostActionExecutor(
                bot=bot,
                session_factory=Session,
            )
            hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
                executor=_NoopAuxiliaryExecutor(),
                post_action_executor=post_actions,
                session_factory=Session,
            )
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                post_send_hook=hook,
                heartbeat_interval_seconds=120,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )

            first = await wrapper.execute(publication_id)
            assert first.outcome == "published"
            assert sender.calls == 1
            assert bot.pins == [
                {"chat_id": source_chat_id, "message_id": 4102}
            ]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, task_id) is None
                actions = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(actions) == 1
                assert actions[0].action_key == "pin:4102"
                assert actions[0].state == "succeeded"

            second = await wrapper.execute(publication_id)
            assert second.outcome == "ineligible"
            assert sender.calls == 1
            assert len(bot.pins) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
