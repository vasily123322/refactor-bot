from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.published_notice_links import (
    legacy_published_notice_callback,
    published_notice_open_callback,
)


async def _seed(Session) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=77001,
            username="owner",
            full_name="Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10077001,
            title="Published notice",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Notice content"}]
            ),
            created_by_tg_user_id=77001,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id)
        )
        return (
            int(publication.legacy_post_task_id or 0),
            int(publication.id),
            int(publication.schedule_entry_id or 0),
        )


def test_notice_uses_publication_identity_before_terminal_projection(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'published-notice-links.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            task_id, publication_id, _ = await _seed(Session)

            async with Session() as session:
                post = await session.get(PostTask, task_id)
                assert post is not None
                # Notification is emitted before terminal projection. The resolver must
                # still use canonical identity while Publication/Schedule are queued.
                callback = await published_notice_open_callback(
                    session,
                    post=post,
                    date_iso="2026-08-10",
                )

            assert callback == f"cp_open_pub:{publication_id}:2026-08-10"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_corrupt_notice_linkage_falls_back_to_legacy_callback(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'published-notice-link-guard.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            task_id, _, schedule_id = await _seed(Session)

            async with Session() as session:
                post = await session.get(PostTask, task_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert post is not None and schedule is not None
                schedule.channel_id = int(post.channel_id) + 999
                await session.commit()

                callback = await published_notice_open_callback(
                    session,
                    post=post,
                    date_iso="2026-08-10",
                )

            assert callback == f"cp_open_post:{task_id}:2026-08-10"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_notice_lookup_failure_and_malformed_id_fail_soft() -> None:
    class FailingSession:
        async def execute(self, statement):
            raise RuntimeError("provider-or-db-secret-like-detail")

    async def run() -> None:
        callback = await published_notice_open_callback(
            FailingSession(),  # type: ignore[arg-type]
            post=SimpleNamespace(id=44, channel_id=12),  # type: ignore[arg-type]
            date_iso="2026-08-10",
        )
        assert callback == "cp_open_post:44:2026-08-10"

    asyncio.run(run())
    assert legacy_published_notice_callback("not-an-id", "2026-08-10") == (
        "cp_open_post:0:2026-08-10"
    )


def test_base_scheduler_hook_remains_legacy() -> None:
    async def run() -> None:
        from app.workers.scheduler import Scheduler as BaseScheduler

        callback = await BaseScheduler._published_notice_open_callback(  # noqa: SLF001
            object(),
            object(),
            SimpleNamespace(id=55),
            "2026-08-10",
        )
        assert callback == "cp_open_post:55:2026-08-10"

    asyncio.run(run())


def test_publication_scheduler_override_delegates_to_canonical_resolver(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.services import published_notice_links as links_module
        from app.workers.publication_scheduler import Scheduler as PublicationScheduler

        calls: list[tuple[object, object, str]] = []

        async def fake_resolver(session, *, post, date_iso: str) -> str:
            calls.append((session, post, date_iso))
            return "cp_open_pub:99:2026-08-10"

        monkeypatch.setattr(links_module, "published_notice_open_callback", fake_resolver)
        session = object()
        post = SimpleNamespace(id=55, channel_id=12)
        callback = await PublicationScheduler._published_notice_open_callback(  # noqa: SLF001
            object(),
            session,
            post,  # type: ignore[arg-type]
            "2026-08-10",
        )
        assert callback == "cp_open_pub:99:2026-08-10"
        assert calls == [(session, post, "2026-08-10")]

    asyncio.run(run())
