from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryAction, PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
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
from app.services.canonical_publication_legacy_transport_handoff import CUTOVER_META_KEY
from app.services.canonical_publication_linked_forward_atomic_handoff import (
    CanonicalPublicationLinkedForwardAtomicHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_linked_forward(Session, *, seed: int):
    async with Session() as session:
        owner = Client(
            tg_user_id=197000 + seed,
            username=f"linked-forward-{seed}",
            full_name=f"Linked Forward {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100197000 + seed),
            title=f"Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target1 = Channel(
            tg_chat_id=-(100297000 + seed),
            title=f"Forward Target A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target2 = Channel(
            tg_chat_id=-(100397000 + seed),
            title=f"Forward Target B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target1, target2])
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Linked forward atomic proof {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={
                "silent": True,
                "forward_to": [int(target1.id), int(target2.id)],
            },
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "source_channel_id": int(source.id),
            "source_tg": int(source.tg_chat_id),
            "target1_id": int(target1.id),
            "target1_tg": int(target1.tg_chat_id),
            "target2_id": int(target2.id),
            "target2_tg": int(target2.tg_chat_id),
        }


def test_linked_forward_atomic_claim_commits_retirement_claim_and_target_snapshot(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-forward-atomic.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed_linked_forward(Session, seed=1)

            async with Session() as session:
                result = await CanonicalPublicationLinkedForwardAtomicHandoffService(
                    session
                ).claim_linked_forward(
                    seeded["publication_id"],
                    holder="linked-forward-atomic",
                    ttl_seconds=180,
                )
                assert result.outcome == "claimed"
                assert result.claim is not None
                assert result.claim.plan.runtime_options() == {
                    "silent": True,
                    "forward_to": [seeded["target1_id"], seeded["target2_id"]],
                }

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                assert publication is not None
                assert publication.status == "sending"
                assert int(publication.attempt_count or 0) == 1
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, seeded["task_id"]) is None
                assert (
                    await session.get(PublicationDeliveryLease, seeded["publication_id"])
                    is not None
                )
                marker = dict(publication.meta or {})[CUTOVER_META_KEY]
                assert marker["atomic_claim"] is True
                assert marker["forward_to"] == [
                    seeded["target1_id"],
                    seeded["target2_id"],
                ]

                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                assert schedule is not None
                assert dict(schedule.meta or {})[CUTOVER_META_KEY]["forward_to"] == [
                    seeded["target1_id"],
                    seeded["target2_id"],
                ]

                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == seeded["publication_id"],
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert dict(attempt.meta or {})[FORWARD_TARGET_SNAPSHOT_META_KEY] == [
                    {
                        "channel_id": seeded["target1_id"],
                        "telegram_chat_id": seeded["target1_tg"],
                    },
                    {
                        "channel_id": seeded["target2_id"],
                        "telegram_chat_id": seeded["target2_tg"],
                    },
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [4101, 4102]


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


class _ForwardBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def forward_message(self, **kwargs):
        self.calls.append(dict(kwargs))
        return None


def test_linked_forward_end_to_end_preserves_order_silent_and_never_replays(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-forward-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed_linked_forward(Session, seed=2)
            sender = _Sender()
            bot = _ForwardBot()
            hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
                executor=_NoopAuxiliaryExecutor(),
                post_action_executor=CanonicalPublicationDeliveryLivePostActionExecutor(
                    bot=bot,
                    session_factory=Session,
                ),
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

            first = await wrapper.execute(seeded["publication_id"])
            assert first.outcome == "published"
            assert sender.calls == 1
            assert bot.calls == [
                {
                    "chat_id": seeded["target1_tg"],
                    "from_chat_id": seeded["source_tg"],
                    "message_id": 4101,
                    "disable_notification": True,
                },
                {
                    "chat_id": seeded["target1_tg"],
                    "from_chat_id": seeded["source_tg"],
                    "message_id": 4102,
                    "disable_notification": True,
                },
                {
                    "chat_id": seeded["target2_tg"],
                    "from_chat_id": seeded["source_tg"],
                    "message_id": 4101,
                    "disable_notification": True,
                },
                {
                    "chat_id": seeded["target2_tg"],
                    "from_chat_id": seeded["source_tg"],
                    "message_id": 4102,
                    "disable_notification": True,
                },
            ]

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                assert publication is not None
                assert publication.status == "published"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, seeded["task_id"]) is None
                actions = (
                    await session.execute(
                        select(PublicationDeliveryAction)
                        .where(
                            PublicationDeliveryAction.publication_id
                            == seeded["publication_id"]
                        )
                        .order_by(PublicationDeliveryAction.action_key.asc())
                    )
                ).scalars().all()
                assert len(actions) == 4
                assert all(str(action.action_type) == "forward" for action in actions)
                assert all(str(action.state) == "succeeded" for action in actions)

            second = await wrapper.execute(seeded["publication_id"])
            assert second.outcome == "ineligible"
            assert sender.calls == 1
            assert len(bot.calls) == 4
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_forward_target_never_retires_legacy_or_calls_primary(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-forward-missing-target.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed_linked_forward(Session, seed=3)

            async with Session() as session:
                target = await session.get(Channel, seeded["target2_id"])
                assert target is not None
                await session.delete(target)
                await session.commit()

            sender = _Sender()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                heartbeat_interval_seconds=120,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )
            result = await wrapper.execute(seeded["publication_id"])
            assert result.outcome == "ineligible"
            assert sender.calls == 0

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                task = await session.get(PostTask, seeded["task_id"])
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert publication.legacy_post_task_id == seeded["task_id"]
                assert task is not None and task.status == "pending"
                assert (
                    await session.get(PublicationDeliveryLease, seeded["publication_id"])
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
