from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteAction
from app.domain.publication_delivery import PublicationDeliveryAction
from app.domain.publishing.models import Publication, PublicationAttempt
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
from app.services.canonical_publication_delivery_runtime import (
    CanonicalPublicationDeliveryLiveAutodeleteCoordinator,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.canonical_repeat_time_pin_autodelete import (
    CanonicalRepeatTimePinAutodeleteService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed(Session, *, seed: int) -> dict[str, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=247000 + seed,
            username=f"repeat-time-pin-e2e-{seed}",
            full_name=f"Repeat Time Pin E2E {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100247000 + seed),
            title=f"Repeat Time Pin E2E {seed}",
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
                        "text": "repeat + time + pin e2e",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "autodelete_seconds": 90,
            },
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "content_item_id": int(publication.content_item_id),
            "channel_id": int(publication.channel_id),
            "telegram_chat_id": int(channel.tg_chat_id),
        }


class _Sender:
    def __init__(self, message_id: int) -> None:
        self.message_id = int(message_id)
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [self.message_id]


class _PinBot:
    def __init__(self) -> None:
        self.pins: list[dict[str, int]] = []

    async def pin_chat_message(self, **kwargs) -> None:
        self.pins.append(
            {
                "chat_id": int(kwargs["chat_id"]),
                "message_id": int(kwargs["message_id"]),
            }
        )

    async def forward_message(self, **kwargs) -> None:
        raise AssertionError("repeat+time+pin profile must not forward")


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


class _DeleteProvider:
    def __init__(
        self,
        *,
        Session,
        publication_id: int,
        fail: bool = False,
    ) -> None:
        self.Session = Session
        self.publication_id = int(publication_id)
        self.fail = bool(fail)
        self.delete_calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        # Observe the committed destructive linearization point from another session.
        async with self.Session() as session:
            action = (
                await session.execute(
                    select(PublicationAutodeleteAction).where(
                        PublicationAutodeleteAction.publication_id
                        == self.publication_id,
                        PublicationAutodeleteAction.telegram_message_id
                        == int(message_id),
                    )
                )
            ).scalar_one()
            assert str(action.state) == "reserved"
            assert int(action.telegram_chat_id) == int(chat_id)
        self.delete_calls.append((int(chat_id), int(message_id)))
        if self.fail:
            raise RuntimeError("ambiguous provider boundary")

    async def send_message(self, **kwargs) -> None:
        raise AssertionError("test profile does not request delete reports")


async def _deliver(
    Session,
    seeded: dict[str, int],
    *,
    message_id: int,
):
    sender = _Sender(message_id)
    pin_bot = _PinBot()
    timer_writer = CanonicalPublicationDeliveryLiveAutodeleteCoordinator(
        session_factory=Session,
    )
    post_actions = CanonicalPublicationDeliveryLivePostActionExecutor(
        bot=pin_bot,
        session_factory=Session,
    )
    hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=_NoopAuxiliaryExecutor(),
        autodelete_writer=timer_writer,
        post_action_executor=post_actions,
        session_factory=Session,
        repeat_owner_policy_enforced=True,
    )
    delegate = CanonicalPublicationSafeRepeatDeliveryExecutor(
        Session,
        sender=sender,
        post_send_hook=hook,
        allow_time_autodelete=True,
        allow_repeat=True,
        allow_repeat_time=True,
        allow_repeat_time_pin=True,
        heartbeat_interval_seconds=120,
    )
    router = CanonicalPublicationRepeatHandoffExecutor(
        executor=delegate,
        session_factory=Session,
    )

    result = await router.execute(seeded["publication_id"])
    assert result.outcome == "published"
    assert sender.calls == 1
    assert pin_bot.pins == [
        {
            "chat_id": seeded["telegram_chat_id"],
            "message_id": message_id,
        }
    ]

    async with Session() as session:
        source = await session.get(Publication, seeded["publication_id"])
        assert source is not None
        assert source.status == "published"
        assert source.legacy_post_task_id is None
        assert source.telegram_message_ids == [message_id]
        assert await session.get(PostTask, seeded["task_id"]) is None

        runtime = dict(source.meta or {})[AUTODELETE_RUNTIME_META_KEY]
        assert runtime["deleted"] is False
        assert int(runtime["effective_seconds"]) == 90
        due_at = datetime.fromisoformat(str(runtime["scheduled_at"]))

        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == seeded["publication_id"],
                    PublicationAttempt.attempt == 1,
                )
            )
        ).scalar_one()
        assert attempt.status == "published"
        assert attempt.telegram_message_ids == [message_id]
        assert dict(attempt.meta or {}).get("canonical_delivery") is True

        pin_actions = (
            await session.execute(
                select(PublicationDeliveryAction).where(
                    PublicationDeliveryAction.publication_id
                    == seeded["publication_id"]
                )
            )
        ).scalars().all()
        assert len(pin_actions) == 1
        assert pin_actions[0].action_key == f"pin:{message_id}"
        assert pin_actions[0].state == "succeeded"

    return router, sender, pin_bot, due_at


async def _assert_exactly_one_pristine_successor(
    Session,
    seeded: dict[str, int],
) -> int:
    async with Session() as session:
        successors = (
            await session.execute(
                select(Publication).where(
                    Publication.id != seeded["publication_id"],
                    Publication.content_item_id == seeded["content_item_id"],
                    Publication.channel_id == seeded["channel_id"],
                )
            )
        ).scalars().all()
        assert len(successors) == 1
        successor = successors[0]
        assert successor.status == "queued"
        assert successor.telegram_message_ids in (None, [])
        assert AUTODELETE_RUNTIME_META_KEY not in dict(successor.meta or {})
        assert dict(successor.meta or {}).get("runtime_options") == {
            "silent": True,
            "pin_on": True,
            "autodelete_seconds": 90,
        }
        assert successor.legacy_post_task_id is not None
        task = await session.get(PostTask, int(successor.legacy_post_task_id))
        assert task is not None and task.status == "pending"
        payload = dict(task.payload or {})
        assert payload.get("repeat_on") is True
        assert payload.get("repeat_seconds") == 60
        assert payload.get("pin_on") is True
        assert payload.get("autodelete_seconds") == 90

        assert (
            await session.execute(
                select(PublicationDeliveryAction).where(
                    PublicationDeliveryAction.publication_id == int(successor.id)
                )
            )
        ).scalars().all() == []
        assert (
            await session.execute(
                select(PublicationAutodeleteAction).where(
                    PublicationAutodeleteAction.publication_id == int(successor.id)
                )
            )
        ).scalars().all() == []
        return int(successor.id)


async def _materialize_successor(Session, seeded: dict[str, int]):
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
    successor_id = await _assert_exactly_one_pristine_successor(Session, seeded)
    return continuation, successor_id


def test_repeat_time_pin_clean_lifecycle_and_replays_are_single_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-clean-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1)
            message_id = 9501
            router, sender, pin_bot, due_at = await _deliver(
                Session,
                seeded,
                message_id=message_id,
            )
            continuation, successor_id = await _materialize_successor(Session, seeded)

            provider = _DeleteProvider(
                Session=Session,
                publication_id=seeded["publication_id"],
            )
            async with Session() as session:
                deleted = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(
                    seeded["publication_id"],
                    now=due_at + timedelta(seconds=1),
                )
            assert deleted.outcome == "deleted"
            assert deleted.deleted_count == 1
            assert provider.delete_calls == [
                (seeded["telegram_chat_id"], message_id)
            ]

            async with Session() as session:
                source = await session.get(Publication, seeded["publication_id"])
                assert source is not None
                runtime = dict(source.meta or {})[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["deleted"] is True
                delete_action = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id
                            == seeded["publication_id"]
                        )
                    )
                ).scalar_one()
                assert str(delete_action.state) == "succeeded"

            replay_provider = _DeleteProvider(
                Session=Session,
                publication_id=seeded["publication_id"],
            )
            async with Session() as session:
                replay_delete = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(
                    seeded["publication_id"],
                    now=due_at + timedelta(seconds=2),
                )
            assert replay_delete.outcome in {"ineligible", "already_deleted"}
            assert replay_provider.delete_calls == []

            replay_delivery = await router.execute(seeded["publication_id"])
            assert replay_delivery.outcome == "ineligible"
            assert sender.calls == 1
            assert pin_bot.pins == [
                {
                    "chat_id": seeded["telegram_chat_id"],
                    "message_id": message_id,
                }
            ]

            second_tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert second_tick.materialized == 0
            assert second_tick.conflicts == 0
            assert await _assert_exactly_one_pristine_successor(Session, seeded) == successor_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_ambiguous_delete_keeps_pin_and_successor_single_effect(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-ambiguous-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2)
            message_id = 9502
            router, sender, pin_bot, due_at = await _deliver(
                Session,
                seeded,
                message_id=message_id,
            )
            continuation, successor_id = await _materialize_successor(Session, seeded)

            provider = _DeleteProvider(
                Session=Session,
                publication_id=seeded["publication_id"],
                fail=True,
            )
            async with Session() as session:
                first = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(
                    seeded["publication_id"],
                    now=due_at + timedelta(seconds=1),
                )
            assert first.outcome == "ambiguous"
            assert provider.delete_calls == [
                (seeded["telegram_chat_id"], message_id)
            ]

            async with Session() as session:
                source = await session.get(Publication, seeded["publication_id"])
                assert source is not None
                runtime = dict(source.meta or {})[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["deleted"] is False
                action = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id
                            == seeded["publication_id"]
                        )
                    )
                ).scalar_one()
                assert str(action.state) == "unknown"

            replay_provider = _DeleteProvider(
                Session=Session,
                publication_id=seeded["publication_id"],
            )
            async with Session() as session:
                replay = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(
                    seeded["publication_id"],
                    now=due_at + timedelta(seconds=2),
                )
            assert replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []

            replay_delivery = await router.execute(seeded["publication_id"])
            assert replay_delivery.outcome == "ineligible"
            assert sender.calls == 1
            assert pin_bot.pins == [
                {
                    "chat_id": seeded["telegram_chat_id"],
                    "message_id": message_id,
                }
            ]

            second_tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert second_tick.materialized == 0
            assert second_tick.conflicts == 0
            assert await _assert_exactly_one_pristine_successor(Session, seeded) == successor_id
        finally:
            await engine.dispose()

    asyncio.run(run())
