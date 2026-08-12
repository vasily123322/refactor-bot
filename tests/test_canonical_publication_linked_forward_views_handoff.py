from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publication_delivery import PublicationDeliveryLease
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
from app.services.publication_autodelete_views import PublicationAutodeleteViewsService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed(Session, *, seed: int, dual_delete: bool = False) -> dict[str, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=203000 + seed,
            username=f"forward-views-{seed}",
            full_name=f"Forward Views {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100203000 + seed),
            title=f"Forward Views Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100303000 + seed),
            title=f"Forward Views Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()
        runtime_options = {
            "silent": True,
            "pin_on": True,
            "forward_to": [int(target.id)],
            "autodelete_views": 3,
            "autodelete_report": False,
        }
        if dual_delete:
            runtime_options["autodelete_seconds"] = 60
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Forward views composition {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=runtime_options,
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "source_tg": int(source.tg_chat_id),
            "target_tg": int(target.tg_chat_id),
        }


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        assert kwargs.get("disable_notification") is True
        return [5601]


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


class _Bot:
    def __init__(self) -> None:
        self.post_actions: list[tuple[str, dict]] = []
        self.delete_calls: list[dict] = []
        self.report_calls: list[dict] = []

    async def pin_chat_message(self, **kwargs):
        self.post_actions.append(("pin", dict(kwargs)))
        return None

    async def forward_message(self, **kwargs):
        self.post_actions.append(("forward", dict(kwargs)))
        return None

    async def delete_message(self, **kwargs):
        self.delete_calls.append(dict(kwargs))
        return None

    async def send_message(self, **kwargs):
        self.report_calls.append(dict(kwargs))
        return None


class _ViewsSource:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return 3


def _hook(Session, bot: _Bot):
    return CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=_NoopAuxiliaryExecutor(),
        post_action_executor=CanonicalPublicationDeliveryLivePostActionExecutor(
            bot=bot,
            session_factory=Session,
        ),
        session_factory=Session,
    )


def test_forward_views_requires_views_worker_and_preserves_linked_transport_when_absent(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-views-disabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1)
            sender = _Sender()
            bot = _Bot()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                post_send_hook=_hook(Session, bot),
                allow_views_autodelete=False,
                heartbeat_interval_seconds=120,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )

            result = await wrapper.execute(seeded["publication_id"])
            assert result.outcome == "ineligible"
            assert sender.calls == 0
            assert bot.post_actions == []

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                task = await session.get(PostTask, seeded["task_id"])
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == seeded["task_id"]
                assert task is not None and task.status == "pending"
                assert (
                    await session.get(
                        PublicationAutodeleteViewState,
                        seeded["publication_id"],
                    )
                    is None
                )
                assert (
                    await session.get(PublicationDeliveryLease, seeded["publication_id"])
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_forward_views_publishes_pin_forwards_then_deletes_source_at_threshold(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-views-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2)
            sender = _Sender()
            bot = _Bot()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                post_send_hook=_hook(Session, bot),
                allow_views_autodelete=True,
                heartbeat_interval_seconds=120,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )

            first = await wrapper.execute(seeded["publication_id"])
            assert first.outcome == "published"
            assert sender.calls == 1
            assert bot.post_actions == [
                (
                    "pin",
                    {"chat_id": seeded["source_tg"], "message_id": 5601},
                ),
                (
                    "forward",
                    {
                        "chat_id": seeded["target_tg"],
                        "from_chat_id": seeded["source_tg"],
                        "message_id": 5601,
                        "disable_notification": True,
                    },
                ),
            ]

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                state = await session.get(
                    PublicationAutodeleteViewState,
                    seeded["publication_id"],
                )
                assert publication is not None
                assert publication.status == "published"
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, seeded["task_id"]) is None
                assert state is not None and int(state.threshold) == 3

            source = _ViewsSource()
            async with Session() as session:
                deleted = await PublicationAutodeleteViewsService(
                    session,
                    view_source=source,
                    delete_provider=bot,
                    allow_report=False,
                ).evaluate_and_delete(
                    seeded["publication_id"],
                    now=datetime.now(timezone.utc) + timedelta(minutes=1),
                )
                assert deleted.outcome == "deleted"

            assert source.calls == [(seeded["source_tg"], 5601)]
            assert bot.delete_calls == [
                {"chat_id": seeded["source_tg"], "message_id": 5601}
            ]
            assert bot.report_calls == []

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                assert publication is not None
                runtime = dict(publication.meta or {})[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["mode"] == "views"
                assert runtime["view_threshold"] == 3
                assert runtime["deleted"] is True
                assert (
                    await session.get(
                        PublicationAutodeleteViewState,
                        seeded["publication_id"],
                    )
                    is None
                )

            second = await wrapper.execute(seeded["publication_id"])
            assert second.outcome == "ineligible"
            assert sender.calls == 1
            assert len(bot.post_actions) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_forward_time_and_views_dual_trigger_remains_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-dual-delete.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=3, dual_delete=True)
            sender = _Sender()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                allow_time_autodelete=True,
                allow_views_autodelete=True,
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
                assert publication.legacy_post_task_id == seeded["task_id"]
                assert task is not None and task.status == "pending"
                assert (
                    await session.get(
                        PublicationAutodeleteViewState,
                        seeded["publication_id"],
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
