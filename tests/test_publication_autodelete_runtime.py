from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import (
    AUTODELETE_RUNTIME_META_KEY,
    normalize_autodelete_runtime,
)
from app.workers import reliable_scheduler
from app.workers.publication_scheduler import Scheduler


class _Bot:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.deleted.append((int(chat_id), int(message_id)))
        return True

    async def get_chat(self, chat_id: int):
        return type("Chat", (), {"username": None})()

    async def send_message(self, *args, **kwargs):
        return True

    async def pin_chat_message(self, *args, **kwargs):
        return True

    async def forward_message(self, *args, **kwargs):
        return True


class _Posting:
    def __init__(self, bot: _Bot) -> None:
        self.bot = bot

    async def send_now(self, *args, **kwargs):
        return [1]


async def _seed(Session, *, tg_user_id: int, tg_chat_id: int):
    async with Session() as session:
        client = await ClientsRepo(session).create_or_get(
            tg_user_id,
            f"user_{tg_user_id}",
            "Autodelete Test",
        )
        channel = await ChannelsRepo(session).create(
            int(client.id),
            tg_chat_id,
            "Autodelete",
        )
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Delete later"}]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id)
        )
        return (
            int(channel.id),
            int(channel.tg_chat_id),
            int(publication.id),
            int(publication.legacy_post_task_id or 0),
        )


def test_normalize_autodelete_runtime_is_sanitized_and_transport_neutral() -> None:
    state = normalize_autodelete_runtime(
        {
            "autodelete_seconds": "60",
            "autodelete_effective_seconds": "90",
            "autodelete_at": "2026-08-10T01:02:03Z",
            "autodeleted": True,
            "autodeleted_at": "2026-08-10T01:03:33+00:00",
            "result_ids": [1, 2],
            "secret": "must-not-copy",
        }
    )
    assert state == {
        "deleted": True,
        "effective_seconds": 90,
        "scheduled_at": "2026-08-10T01:02:03+00:00",
        "deleted_at": "2026-08-10T01:03:33+00:00",
    }
    assert normalize_autodelete_runtime({"autodelete_seconds": -1}) is None
    assert normalize_autodelete_runtime({"autodelete_at": "not-a-date"}) is None
    assert normalize_autodelete_runtime({"autodeleted": "true"}) is None


def test_normal_publication_projection_mirrors_scheduled_autodelete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-projection.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, _, publication_id, task_id = await _seed(
                Session,
                tg_user_id=10001,
                tg_chat_id=-100100010001,
            )
            due = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [501],
                    "autodelete_seconds": 120,
                    "autodelete_effective_seconds": 120,
                    "autodelete_at": due.isoformat(),
                }
                await session.commit()

            scheduler = Scheduler(Session, _Posting(_Bot()))
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                await scheduler._project_publication(session, task)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY] == {
                    "deleted": False,
                    "effective_seconds": 120,
                    "scheduled_at": due.isoformat(),
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_local_timer_projects_deleted_runtime_after_posttask_update(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-local-timer.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, tg_chat_id, publication_id, task_id = await _seed(
                Session,
                tg_user_id=10002,
                tg_chat_id=-100100020002,
            )
            due = datetime.now(timezone.utc) + timedelta(minutes=5)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [601],
                    "autodelete_seconds": 300,
                    "autodelete_effective_seconds": 300,
                    "autodelete_at": due.isoformat(),
                }
                await session.commit()

            bot = _Bot()
            scheduler = Scheduler(Session, _Posting(bot))
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                await scheduler._project_publication(session, task)

            # ReliableScheduler's legacy timed callback still persists through its
            # module-level factory. Point that compatibility seam at this test DB.
            monkeypatch.setattr(reliable_scheduler, "AsyncSessionLocal", Session)
            await scheduler._del_later(
                scheduler.posting.bot,
                tg_chat_id,
                [601],
                0,
                task_id,
                False,
                None,
            )

            assert bot.deleted == [(tg_chat_id, 601)]
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = await session.get(Publication, publication_id)
                assert task is not None
                assert task.payload["autodeleted"] is True
                assert task.payload.get("autodeleted_at")
                assert publication is not None
                runtime = publication.meta[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["deleted"] is True
                assert runtime["effective_seconds"] == 300
                assert runtime["scheduled_at"] == due.isoformat()
                assert runtime.get("deleted_at")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fallback_deleter_projects_deleted_runtime_without_reimplementing_delete_logic(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-fallback.db'}"
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, tg_chat_id, publication_id, task_id = await _seed(
                Session,
                tg_user_id=10003,
                tg_chat_id=-100100030003,
            )
            due = datetime.now(timezone.utc) - timedelta(seconds=1)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [701],
                    "autodelete_seconds": 1,
                    "autodelete_effective_seconds": 1,
                    "autodelete_at": due.isoformat(),
                    "autodelete_report": False,
                }
                await session.commit()

            bot = _Bot()
            scheduler = Scheduler(Session, _Posting(bot))
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                await scheduler._project_publication(session, task)

            async with Session() as session:
                await scheduler._process_due_deletions(session)

            assert bot.deleted == [(tg_chat_id, 701)]
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                publication = await session.get(Publication, publication_id)
                assert task is not None and task.payload["autodeleted"] is True
                assert publication is not None
                runtime = publication.meta[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["deleted"] is True
                assert runtime["effective_seconds"] == 1
                assert runtime["scheduled_at"] == due.isoformat()
                assert runtime.get("deleted_at")
        finally:
            await engine.dispose()

    asyncio.run(run())
