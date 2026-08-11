from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryAction, PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_autodelete_runtime_planner import (
    CanonicalPublicationAutodeleteRuntimePlanner,
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
from app.services.canonical_publication_delivery_runtime import (
    CanonicalPublicationDeliveryLiveAutodeleteCoordinator,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed(Session, *, seed: int) -> dict[str, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=200000 + seed,
            username=f"forward-time-{seed}",
            full_name=f"Forward Time {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100200000 + seed),
            title=f"Forward Time Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100300000 + seed),
            title=f"Forward Time Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Forward time composition {seed}",
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
                "pin_on": True,
                "forward_to": [int(target.id)],
                "autodelete_seconds": 60,
                "autodelete_views": 0,
                "autodelete_report": True,
            },
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "source_tg": int(source.tg_chat_id),
            "target_tg": int(target.tg_chat_id),
        }


class _Sender:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        self.events.append("primary")
        assert kwargs.get("disable_notification") is True
        return [4501, 4502]


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


class _Bot:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[str, dict]] = []

    async def pin_chat_message(self, **kwargs):
        self.events.append("pin")
        self.calls.append(("pin", dict(kwargs)))
        return None

    async def forward_message(self, **kwargs):
        self.events.append("forward")
        self.calls.append(("forward", dict(kwargs)))
        return None


class _RecordingTimer:
    def __init__(self, events: list[str], delegate) -> None:
        self.events = events
        self.delegate = delegate
        self.calls = 0

    async def materialize(self, context):
        self.calls += 1
        self.events.append("timer")
        return await self.delegate.materialize(context)


class _ConflictTimer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    async def materialize(self, context):
        self.calls += 1
        self.events.append("timer")
        return SimpleNamespace(
            publication_id=int(context.publication_id),
            outcome="conflict",
        )


def test_linked_forward_time_composition_materializes_timer_before_pin_and_forward(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-time-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1)
            events: list[str] = []
            sender = _Sender(events)
            bot = _Bot(events)
            timer = _RecordingTimer(
                events,
                CanonicalPublicationDeliveryLiveAutodeleteCoordinator(
                    session_factory=Session,
                ),
            )
            hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
                executor=_NoopAuxiliaryExecutor(),
                autodelete_writer=timer,
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
                allow_time_autodelete=True,
                heartbeat_interval_seconds=120,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )

            first = await wrapper.execute(seeded["publication_id"])
            assert first.outcome == "published"
            assert sender.calls == 1
            assert timer.calls == 1
            assert events == ["primary", "timer", "pin", "forward", "forward"]
            assert bot.calls == [
                (
                    "pin",
                    {
                        "chat_id": seeded["source_tg"],
                        "message_id": 4502,
                    },
                ),
                (
                    "forward",
                    {
                        "chat_id": seeded["target_tg"],
                        "from_chat_id": seeded["source_tg"],
                        "message_id": 4501,
                        "disable_notification": True,
                    },
                ),
                (
                    "forward",
                    {
                        "chat_id": seeded["target_tg"],
                        "from_chat_id": seeded["source_tg"],
                        "message_id": 4502,
                        "disable_notification": True,
                    },
                ),
            ]

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                assert publication is not None
                assert publication.status == "published"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, seeded["task_id"]) is None
                runtime = dict(publication.meta or {}).get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict)
                assert runtime["deleted"] is False
                assert runtime["effective_seconds"] == 60
                terminal = await CanonicalPublicationAutodeleteRuntimePlanner(
                    session
                ).plan(seeded["publication_id"])
                assert terminal is not None
                assert terminal.existing is True

                actions = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id
                            == seeded["publication_id"]
                        )
                    )
                ).scalars().all()
                assert len(actions) == 3
                assert all(str(action.state) == "succeeded" for action in actions)

            second = await wrapper.execute(seeded["publication_id"])
            assert second.outcome == "ineligible"
            assert sender.calls == 1
            assert timer.calls == 1
            assert len(bot.calls) == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_forward_timer_conflict_blocks_pin_forward_and_primary_replay(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-time-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2)
            events: list[str] = []
            sender = _Sender(events)
            bot = _Bot(events)
            timer = _ConflictTimer(events)
            hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
                executor=_NoopAuxiliaryExecutor(),
                autodelete_writer=timer,
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
                allow_time_autodelete=True,
                heartbeat_interval_seconds=120,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )

            first = await wrapper.execute(seeded["publication_id"])
            assert first.outcome == "lease_lost"
            assert sender.calls == 1
            assert timer.calls == 1
            assert events == ["primary", "timer"]
            assert bot.calls == []

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids is None
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, seeded["task_id"]) is None
                assert (
                    await session.get(PublicationDeliveryLease, seeded["publication_id"])
                    is not None
                )
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == seeded["publication_id"],
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert attempt.finished_at is None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                actions = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id
                            == seeded["publication_id"]
                        )
                    )
                ).scalars().all()
                assert actions == []

            second = await wrapper.execute(seeded["publication_id"])
            assert second.outcome == "ineligible"
            assert sender.calls == 1
            assert timer.calls == 1
            assert bot.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
