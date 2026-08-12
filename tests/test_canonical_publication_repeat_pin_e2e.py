from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryAction
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecution,
)
from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)
from app.services.canonical_publication_delivery_live_post_action_executor import (
    CanonicalPublicationDeliveryLivePostActionExecutor,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed(Session) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=210001,
            username="repeat-pin-e2e",
            full_name="Repeat Pin E2E",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100210001,
            title="Repeat Pin E2E",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat + pin e2e"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"silent": True, "pin_on": True},
        )
        assert publication.legacy_post_task_id is not None
        return (
            int(publication.id),
            int(publication.legacy_post_task_id),
            int(channel.tg_chat_id),
        )


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [7201, 7202]


class _PinBot:
    def __init__(self) -> None:
        self.pins: list[dict] = []

    async def pin_chat_message(self, **kwargs) -> None:
        self.pins.append(dict(kwargs))

    async def forward_message(self, **kwargs) -> None:
        raise AssertionError("repeat+pin profile must not forward")


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


def test_repeat_pin_atomic_publish_continuation_and_replay_are_single_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-pin-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, source_chat_id = await _seed(Session)

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
                repeat_owner_policy_enforced=True,
            )
            delegate = CanonicalPublicationSafeRepeatDeliveryExecutor(
                Session,
                sender=sender,
                post_send_hook=hook,
                allow_repeat=True,
                heartbeat_interval_seconds=120,
            )
            router = CanonicalPublicationRepeatHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )

            first = await router.execute(publication_id)
            assert first.outcome == "published"
            assert sender.calls == 1
            assert bot.pins == [{"chat_id": source_chat_id, "message_id": 7202}]

            async with Session() as session:
                source = await session.get(Publication, publication_id)
                assert source is not None
                assert source.status == "published"
                assert source.legacy_post_task_id is None
                assert await session.get(PostTask, task_id) is None
                actions = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(actions) == 1
                assert actions[0].action_key == "pin:7202"
                assert actions[0].state == "succeeded"

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert tick.materialized == 1
            assert tick.conflicts == 0

            async with Session() as session:
                source = await session.get(Publication, publication_id)
                assert source is not None
                successors = (
                    await session.execute(
                        select(Publication).where(
                            Publication.id != publication_id,
                            Publication.content_item_id == int(source.content_item_id),
                            Publication.content_revision == int(source.content_revision),
                            Publication.channel_id == int(source.channel_id),
                        )
                    )
                ).scalars().all()
                assert len(successors) == 1
                successor = successors[0]
                assert successor.status == "queued"
                assert dict(successor.meta or {}).get("runtime_options") == {
                    "silent": True,
                    "pin_on": True,
                }
                assert successor.legacy_post_task_id is not None
                successor_task = await session.get(PostTask, int(successor.legacy_post_task_id))
                assert successor_task is not None
                assert successor_task.status == "pending"
                successor_payload = dict(successor_task.payload or {})
                assert successor_payload.get("pin_on") is True
                assert successor_payload.get("repeat_on") is True
                assert successor_payload.get("repeat_seconds") == 60

            replay = await router.execute(publication_id)
            assert replay.outcome == "ineligible"
            assert sender.calls == 1
            assert bot.pins == [{"chat_id": source_chat_id, "message_id": 7202}]

            second_tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert second_tick.materialized == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
