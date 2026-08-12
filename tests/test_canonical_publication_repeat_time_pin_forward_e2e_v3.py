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
from app.services.canonical_publication_delivery_runtime import (
    CanonicalPublicationDeliveryLiveAutodeleteCoordinator,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.canonical_repeat_time_pin_forward_autodelete import (
    CanonicalRepeatTimePinForwardAutodeleteService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed(Session, seed: int) -> dict[str, object]:
    async with Session() as session:
        owner = Client(
            tg_user_id=259000 + seed,
            username=f"combined-e2e-{seed}",
            full_name="Combined E2E",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100259000 + seed),
            title="Source",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100359000 + seed),
            title="A",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100459000 + seed),
            title="B",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "combined e2e"}]
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
            "source_chat": int(source.tg_chat_id),
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


class _PostActionBot:
    def __init__(self) -> None:
        self.pins: list[tuple[int, int]] = []
        self.forwards: list[tuple[int, int, int, bool]] = []

    async def pin_chat_message(self, **kwargs) -> None:
        self.pins.append((int(kwargs["chat_id"]), int(kwargs["message_id"])))

    async def forward_message(self, **kwargs) -> None:
        self.forwards.append(
            (
                int(kwargs["chat_id"]),
                int(kwargs["from_chat_id"]),
                int(kwargs["message_id"]),
                bool(kwargs["disable_notification"]),
            )
        )


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
        self.calls.append((int(chat_id), int(message_id)))
        if self.fail:
            raise RuntimeError("ambiguous delete")

    async def send_message(self, **kwargs) -> None:
        raise AssertionError("delete report not requested")


async def _deliver(Session, seeded: dict[str, object], message_id: int):
    sender = _Sender(message_id)
    bot = _PostActionBot()
    hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=_NoopAuxiliaryExecutor(),
        autodelete_writer=CanonicalPublicationDeliveryLiveAutodeleteCoordinator(
            session_factory=Session
        ),
        post_action_executor=CanonicalPublicationDeliveryLivePostActionExecutor(
            bot=bot,
            session_factory=Session,
        ),
        session_factory=Session,
        repeat_owner_policy_enforced=True,
    )
    executor = CanonicalPublicationSafeRepeatDeliveryExecutor(
        Session,
        sender=sender,
        post_send_hook=hook,
        allow_time_autodelete=True,
        allow_repeat=True,
        allow_repeat_time=True,
        allow_repeat_time_pin=True,
        allow_repeat_time_forward=True,
        allow_repeat_time_pin_forward=True,
        heartbeat_interval_seconds=120,
    )
    router = CanonicalPublicationRepeatHandoffExecutor(
        executor=executor,
        session_factory=Session,
    )
    result = await router.execute(int(seeded["publication_id"]))
    assert result.outcome == "published"
    assert sender.calls == 1
    assert bot.pins == [(int(seeded["source_chat"]), message_id)]
    assert bot.forwards == [
        (int(chat), int(seeded["source_chat"]), message_id, True)
        for chat in seeded["target_chats"]
    ]

    async with Session() as session:
        source = await session.get(Publication, int(seeded["publication_id"]))
        assert source is not None
        assert source.status == "published"
        assert source.telegram_message_ids == [message_id]
        assert source.legacy_post_task_id is None
        assert await session.get(PostTask, int(seeded["task_id"])) is None
        runtime = dict(source.meta or {})[AUTODELETE_RUNTIME_META_KEY]
        assert runtime["deleted"] is False
        due_at = datetime.fromisoformat(str(runtime["scheduled_at"]))
        actions = (
            await session.execute(
                select(PublicationDeliveryAction).where(
                    PublicationDeliveryAction.publication_id == int(seeded["publication_id"])
                )
            )
        ).scalars().all()
        assert len(actions) == 3
        assert {str(action.action_type) for action in actions} == {"pin", "forward"}
        assert all(str(action.state) == "succeeded" for action in actions)
    return router, sender, bot, due_at


async def _successor(Session, seeded: dict[str, object]) -> tuple[CanonicalRepeatContinuationWorker, int]:
    worker = CanonicalRepeatContinuationWorker(
        session_factory=Session,
        batch_size=10,
        scan_limit=50,
    )
    tick = await worker.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=2))
    assert tick.materialized == 1
    assert tick.conflicts == 0
    async with Session() as session:
        rows = (
            await session.execute(
                select(Publication).where(
                    Publication.id != int(seeded["publication_id"]),
                    Publication.content_item_id == int(seeded["content_item_id"]),
                    Publication.channel_id == int(seeded["channel_id"]),
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        successor = rows[0]
        assert successor.status == "queued"
        assert successor.telegram_message_ids in (None, [])
        assert AUTODELETE_RUNTIME_META_KEY not in dict(successor.meta or {})
        assert dict(successor.meta or {}).get("runtime_options") == {
            "silent": True,
            "pin_on": True,
            "forward_to": list(seeded["target_ids"]),
            "autodelete_seconds": 90,
        }
        assert successor.legacy_post_task_id is not None
        task = await session.get(PostTask, int(successor.legacy_post_task_id))
        assert task is not None and task.status == "pending"
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
        return worker, int(successor.id)


async def _assert_no_second_successor(
    Session,
    seeded: dict[str, object],
    worker: CanonicalRepeatContinuationWorker,
    successor_id: int,
) -> None:
    tick = await worker.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=3))
    assert tick.materialized == 0
    assert tick.conflicts == 0
    async with Session() as session:
        rows = (
            await session.execute(
                select(Publication.id).where(
                    Publication.id != int(seeded["publication_id"]),
                    Publication.content_item_id == int(seeded["content_item_id"]),
                    Publication.channel_id == int(seeded["channel_id"]),
                )
            )
        ).scalars().all()
        assert [int(value) for value in rows] == [successor_id]


def test_combined_clean_lifecycle_and_all_replays_are_single_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-clean-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, 1)
            message_id = 10101
            router, sender, post_bot, due_at = await _deliver(Session, seeded, message_id)
            continuation, successor_id = await _successor(Session, seeded)

            provider = _DeleteProvider(
                Session=Session,
                publication_id=int(seeded["publication_id"]),
            )
            async with Session() as session:
                result = await CanonicalRepeatTimePinForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=1),
                )
            assert result.outcome == "deleted"
            assert provider.calls == [(int(seeded["source_chat"]), message_id)]

            replay = await router.execute(int(seeded["publication_id"]))
            assert replay.outcome == "ineligible"
            assert sender.calls == 1
            assert len(post_bot.pins) == 1
            assert len(post_bot.forwards) == 2

            async with Session() as session:
                replay_provider = _DeleteProvider(
                    Session=Session,
                    publication_id=int(seeded["publication_id"]),
                )
                deleted_again = await CanonicalRepeatTimePinForwardAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_pin_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=2),
                )
                assert deleted_again.outcome in {"ineligible", "already_deleted"}
                assert replay_provider.calls == []
            await _assert_no_second_successor(
                Session,
                seeded,
                continuation,
                successor_id,
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_combined_ambiguous_delete_never_replays_other_effects(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-ambiguous-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, 2)
            message_id = 10102
            router, sender, post_bot, due_at = await _deliver(Session, seeded, message_id)
            continuation, successor_id = await _successor(Session, seeded)

            provider = _DeleteProvider(
                Session=Session,
                publication_id=int(seeded["publication_id"]),
                fail=True,
            )
            async with Session() as session:
                result = await CanonicalRepeatTimePinForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=1),
                )
            assert result.outcome == "ambiguous"
            assert provider.calls == [(int(seeded["source_chat"]), message_id)]

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
                replay_delete = await CanonicalRepeatTimePinForwardAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_pin_forward=True,
                ).delete_if_due(
                    int(seeded["publication_id"]),
                    now=due_at + timedelta(seconds=2),
                )
                assert replay_delete.outcome == "ambiguous"
                assert replay_provider.calls == []

            replay_delivery = await router.execute(int(seeded["publication_id"]))
            assert replay_delivery.outcome == "ineligible"
            assert sender.calls == 1
            assert len(post_bot.pins) == 1
            assert len(post_bot.forwards) == 2
            await _assert_no_second_successor(
                Session,
                seeded,
                continuation,
                successor_id,
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
