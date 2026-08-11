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
from app.services.publication_autodelete_views import PublicationAutodeleteViewsService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_linked_views(Session, *, seed: int, threshold: int = 3) -> dict[str, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=202000 + seed,
            username=f"linked-views-{seed}",
            full_name=f"Linked Views {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100202000 + seed),
            title=f"Linked Views {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Linked views {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={
                "silent": True,
                "autodelete_views": threshold,
                "autodelete_report": False,
            },
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "tg_chat_id": int(channel.tg_chat_id),
        }


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        assert kwargs.get("disable_notification") is True
        return [5501]


class _ViewsSource:
    def __init__(self, views: int) -> None:
        self.views = views
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return self.views


class _DeleteProvider:
    def __init__(self) -> None:
        self.delete_calls: list[dict] = []
        self.report_calls: list[dict] = []

    async def delete_message(self, **kwargs):
        self.delete_calls.append(dict(kwargs))
        return None

    async def send_message(self, **kwargs):
        self.report_calls.append(dict(kwargs))
        return None


def test_linked_views_requires_started_views_executor_and_preserves_legacy_when_absent(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-views-disabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed_linked_views(Session, seed=1, threshold=10)
            sender = _Sender()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
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

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                task = await session.get(PostTask, seeded["task_id"])
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
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


def test_linked_views_publishes_once_then_existing_views_runtime_deletes_at_threshold(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-views-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed_linked_views(Session, seed=2, threshold=3)
            sender = _Sender()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
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

            source = _ViewsSource(views=3)
            delete_provider = _DeleteProvider()
            async with Session() as session:
                deleted = await PublicationAutodeleteViewsService(
                    session,
                    view_source=source,
                    delete_provider=delete_provider,
                    allow_report=False,
                ).evaluate_and_delete(
                    seeded["publication_id"],
                    now=datetime.now(timezone.utc) + timedelta(minutes=1),
                )
                assert deleted.outcome == "deleted"
                assert deleted.threshold == 3
                assert deleted.observed_views == 3
                assert deleted.deleted_count == 1

            assert source.calls == [(seeded["tg_chat_id"], 5501)]
            assert delete_provider.delete_calls == [
                {"chat_id": seeded["tg_chat_id"], "message_id": 5501}
            ]
            assert delete_provider.report_calls == []

            async with Session() as session:
                publication = await session.get(Publication, seeded["publication_id"])
                assert publication is not None
                runtime = dict(publication.meta or {})[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["mode"] == "views"
                assert runtime["view_threshold"] == 3
                assert runtime["observed_views"] == 3
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
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_views_threshold_drift_blocks_atomic_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-views-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed_linked_views(Session, seed=3, threshold=20)

            async with Session() as session:
                task = await session.get(PostTask, seeded["task_id"])
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_views"] = 21
                task.payload = payload
                await session.commit()

            sender = _Sender()
            delegate = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
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
