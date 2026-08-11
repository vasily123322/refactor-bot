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
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
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
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, *, seed: int, runtime_options: dict):
    async with Session() as session:
        owner = Client(
            tg_user_id=199000 + seed,
            username=f"forward-pin-{seed}",
            full_name=f"Forward Pin {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100199000 + seed),
            title=f"Forward Pin Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100299000 + seed),
            title=f"Forward Pin Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()
        options = dict(runtime_options)
        if options.pop("__target__", False):
            options["forward_to"] = [int(target.id)]
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Forward pin {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "source_tg": int(source.tg_chat_id),
            "target_id": int(target.id),
            "target_tg": int(target.tg_chat_id),
        }


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [4301, 4302]


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


class _Bot:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def pin_chat_message(self, **kwargs):
        self.events.append(("pin", dict(kwargs)))
        return None

    async def forward_message(self, **kwargs):
        self.events.append(("forward", dict(kwargs)))
        return None


def test_linked_forward_pin_preserves_legacy_pin_then_forward_order_and_replay_barrier(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-pin-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(
                Session,
                seed=1,
                runtime_options={
                    "__target__": True,
                    "silent": True,
                    "pin_on": True,
                },
            )
            sender = _Sender()
            bot = _Bot()
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
            assert bot.events == [
                (
                    "pin",
                    {
                        "chat_id": seeded["source_tg"],
                        "message_id": 4302,
                    },
                ),
                (
                    "forward",
                    {
                        "chat_id": seeded["target_tg"],
                        "from_chat_id": seeded["source_tg"],
                        "message_id": 4301,
                        "disable_notification": True,
                    },
                ),
                (
                    "forward",
                    {
                        "chat_id": seeded["target_tg"],
                        "from_chat_id": seeded["source_tg"],
                        "message_id": 4302,
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
                actions = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id
                            == seeded["publication_id"]
                        )
                    )
                ).scalars().all()
                assert len(actions) == 3
                assert sorted(str(action.action_type) for action in actions) == [
                    "forward",
                    "forward",
                    "pin",
                ]
                assert all(str(action.state) == "succeeded" for action in actions)

            second = await wrapper.execute(seeded["publication_id"])
            assert second.outcome == "ineligible"
            assert sender.calls == 1
            assert len(bot.events) == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_forward_timer_composition_remains_legacy_owned_even_with_timer_executor(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-timer-still-blocked.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(
                Session,
                seed=2,
                runtime_options={
                    "__target__": True,
                    "pin_on": True,
                    "autodelete_seconds": 60,
                },
            )
            sender = _Sender()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                allow_time_autodelete=True,
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
