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
from app.services.canonical_repeat_time_forward_autodelete import (
    CanonicalRepeatTimeForwardAutodeleteService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed(Session, *, seed: int) -> dict[str, object]:
    async with Session() as session:
        owner = Client(
            tg_user_id=253000 + seed,
            username=f"repeat-time-forward-e2e-{seed}",
            full_name=f"Repeat Time Forward E2E {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100253000 + seed),
            title=f"Repeat Time Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100353000 + seed),
            title=f"Repeat Time Forward A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100453000 + seed),
            title=f"Repeat Time Forward B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time forward e2e"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "forward_to": [int(target_a.id), int(target_b.id)],
                "autodelete_seconds": 90,
            },
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "content_item_id": int(publication.content_item_id),
            "channel_id": int(publication.channel_id),
            "source_chat_id": int(source.tg_chat_id),
            "target_ids": (int(target_a.id), int(target_b.id)),
            "target_chats": (int(target_a.tg_chat_id), int(target_b.tg_chat_id)),
        }


class _Sender:
    def __init__(self, message_id: int) -> None:
        self.message_id = int(message_id)
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [self.message_id]


class _ForwardBot:
    def __init__(self) -> None:
        self.forwards: list[dict[str, object]] = []

    async def forward_message(self, **kwargs) -> None:
        self.forwards.append(
            {
                "chat_id": int(kwargs["chat_id"]),
                "from_chat_id": int(kwargs["from_chat_id"]),
                "message_id": int(kwargs["message_id"]),
                "disable_notification": bool(kwargs["disable_notification"]),
            }
        )

    async def pin_chat_message(self, **kwargs) -> None:
        raise AssertionError("time+forward profile must not pin")


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


class _DeleteProvider:
    def __init__(self, *, Session, publication_id: int, fail: bool = False) -> None:
        self.Session = Session
        self.publication_id = int(publication_id)
        self.fail = bool(fail)
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        async with self.Session() as session:
            action = (
                await session.execute(
                    select(PublicationAutodeleteAction).where(
                        PublicationAutodeleteAction.publication_id == self.publication_id,
                        PublicationAutodeleteAction.telegram_message_id == int(message_id),
                    )
                )
            ).scalar_one()
            assert str(action.state) == "reserved"
            assert int(action.telegram_chat_id) == int(chat_id)
        self.calls.append((int(chat_id), int(message_id)))
        if self.fail:
            raise RuntimeError("ambiguous delete boundary")

    async def send_message(self, **kwargs) -> None:
        raise AssertionError("test profile does not request delete reports")


async def _deliver(Session, seeded: dict[str, object], *, message_id: int):
    sender = _Sender(message_id)
    forward_bot = _ForwardBot()
    timer_writer = CanonicalPublicationDeliveryLiveAutodeleteCoordinator(
        session_factory=Session,
    )
    post_actions = CanonicalPublicationDeliveryLivePostActionExecutor(
        bot=forward_bot,
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
        allow_repeat_time_forward=True,
        heartbeat_interval_seconds=120,
    )
    router = CanonicalPublicationRepeatHandoffExecutor(
        executor=delegate,
        session_factory=Session,
    )

    result = await router.execute(int(seeded["publication_id"]))
    assert result.outcome == "published"
    assert sender.calls == 1
    expected_forwards = [
        {
            "chat_id": int(target_chat),
            "from_chat_id": int(seeded["source_chat_id"]),
            "message_id": message_id,
            "disable_notification": True,
        }
        for target_chat in seeded["target_chats"]
    ]
    assert forward_bot.forwards == expected_forwards

    async with Session() as session:
        source = await session.get(Publication, int(seeded["publication_id"]))
        assert source is not None
        assert source.status == "published"
        assert source.legacy_post_task_id is None
        assert source.telegram_message_ids == [message_id]
        assert await session.get(PostTask, int(seeded["task_id"])) is None
        runtime = dict(source.meta or {})[AUTODELETE_RUNTIME_META_KEY]
        assert runtime["deleted"] is False
        due_at = datetime.fromisoformat(str(runtime["scheduled_at"]))

        actions = (
            await session.execute(
                select(PublicationDeliveryAction).where(
                    PublicationDeliveryAction.publication_id == int(seeded["publication_id"])
                ).order_by(PublicationDeliveryAction.action_key.asc())
            )
        ).scalars().all()
        assert len(actions) == 2
        assert all(str(action.action_type) == "forward" for action in actions)
        assert all(str(action.state) == "succeeded" for action in actions)
        expected_keys = sorted(
            f"forward:{int(target_id)}:{message_id}"
            for target_id in seeded["target_ids"]
        )
        assert [str(action.action_key) for action in actions] == expected_keys

    return router, sender, forward_bot, due_at, expected_forwards


async def _assert_one_pristine_successor(Session, seeded: dict[str, object]) -> int:
    async with Session() as session:
        successors = (
            await session.execute(
                select(Publication).where(
                    Publication.id != int(seeded["publication_id"]),
                    Publication.content_item_id == int(seeded["content_item_id"]),
                    Publication.channel_id == int(seeded["channel_id"]),
                )
            )
        ).scalars().all()
        assert len(successors) == 1
        successor = successors[0]
        assert successor.status == "queued"
        assert successor.telegram_message_ids in (None, [])
        assert AUTODELETE_RUNTIME_META_KEY not in dict(successor.meta or {})
        options = dict(successor.meta or {}).get("runtime_options")
        assert options == {
            "silent": True,
            "forward_to": list(seeded["target_ids"]),
            "autodelete_seconds": 90,
        }
        assert successor.legacy_post_task_id is not None
        task = await session.get(PostTask, int(successor.legacy_post_task_id))
        assert task is not None and task.status == "pending"
        payload = dict(task.payload or {})
        assert payload.get("repeat_on") is True
        assert payload.get("repeat_seconds") == 60
        assert payload.get("forward_to") == list(seeded["target_ids"])
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


async def _materialize_successor(Session, seeded: dict[str, object]):
    worker = CanonicalRepeatContinuationWorker(
        session_factory=Session,
        batch_size=10,
        scan_limit=50,
    )
    tick = await worker.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=2))
    assert tick.materialized == 1
    assert tick.conflicts == 0
    successor_id = await _assert_one_pristine_successor(Session, seeded)
    return worker, successor_id


def test_repeat_time_forward_clean_lifecycle_is_exactly_once(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-clean-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1)
            message_id = 9801
            router, sender, forward_bot, due_at, expected_forwards = await _deliver(
                Session, seeded, message_id=message_id
            )
            continuation, successor_id = await _materialize_successor(Session, seeded)

            provider = _DeleteProvider(
                Session=Session,
                publication_id=int(seeded["publication_id"]),
            )
            async with Session() as session:
                deleted = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=1),
                )
            assert deleted.outcome == "deleted"
            assert provider.calls == [(int(seeded["source_chat_id"]), message_id)]

            replay_provider = _DeleteProvider(
                Session=Session,
                publication_id=int(seeded["publication_id"]),
            )
            async with Session() as session:
                replay = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=2),
                )
            assert replay.outcome in {"ineligible", "already_deleted"}
            assert replay_provider.calls == []

            delivery_replay = await router.execute(int(seeded["publication_id"]))
            assert delivery_replay.outcome == "ineligible"
            assert sender.calls == 1
            assert forward_bot.forwards == expected_forwards

            tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert tick.materialized == 0
            assert tick.conflicts == 0
            assert await _assert_one_pristine_successor(Session, seeded) == successor_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_forward_ambiguous_delete_never_replays_other_effects(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-ambiguous-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2)
            message_id = 9802
            router, sender, forward_bot, due_at, expected_forwards = await _deliver(
                Session, seeded, message_id=message_id
            )
            continuation, successor_id = await _materialize_successor(Session, seeded)

            provider = _DeleteProvider(
                Session=Session,
                publication_id=int(seeded["publication_id"]),
                fail=True,
            )
            async with Session() as session:
                first = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=1),
                )
            assert first.outcome == "ambiguous"
            assert provider.calls == [(int(seeded["source_chat_id"]), message_id)]

            async with Session() as session:
                action = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id
                            == int(seeded["publication_id"])
                        )
                    )
                ).scalar_one()
                assert str(action.state) == "unknown"

            replay_provider = _DeleteProvider(
                Session=Session,
                publication_id=int(seeded["publication_id"]),
            )
            async with Session() as session:
                replay = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=2),
                )
            assert replay.outcome == "ambiguous"
            assert replay_provider.calls == []

            delivery_replay = await router.execute(int(seeded["publication_id"]))
            assert delivery_replay.outcome == "ineligible"
            assert sender.calls == 1
            assert forward_bot.forwards == expected_forwards

            tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert tick.materialized == 0
            assert tick.conflicts == 0
            assert await _assert_one_pristine_successor(Session, seeded) == successor_id
        finally:
            await engine.dispose()

    asyncio.run(run())
