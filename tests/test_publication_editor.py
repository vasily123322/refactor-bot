from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_editor import (
    load_owned_publication_editor_view,
    publication_edit_callback,
    publication_open_callback,
)


async def _seed_owned_publication(Session) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=70001,
            username="owner",
            full_name="Owner",
            is_premium=False,
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10070001,
            title="Canonical channel",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        channel_id = int(channel.id)

    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Canonical edit"}]
            ),
            created_by_tg_user_id=70001,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc),
            timezone_name="Europe/London",
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={"autodelete_seconds": 7200},
        )
        return (
            channel_id,
            int(item.id),
            int(publication.id),
            int(publication.legacy_post_task_id or 0),
        )


def test_canonical_editor_view_survives_post_task_retirement(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-editor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, item_id, publication_id, task_id = await _seed_owned_publication(Session)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.status = "published"
                publication.telegram_message_ids = [81001, 81002]
                publication.result_link = "https://t.me/example/81002"
                # Emulate completed safe transport retirement. This standalone SQLite
                # engine deliberately does not depend on FK cascades.
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                view = await load_owned_publication_editor_view(
                    session,
                    publication_id=publication_id,
                    tg_user_id=70001,
                )
                assert view is not None
                assert view.publication_id == publication_id
                assert view.content_item_id == item_id
                assert view.status == "published"
                assert view.primary_message_id == 81002
                assert view.telegram_message_ids == (81001, 81002)
                assert view.result_link == "https://t.me/example/81002"

                payload = view.editor_payload()
                assert payload["type"] == "text"
                assert payload["text"] == "Canonical edit"
                assert payload["autodelete_seconds"] == 7200
                assert payload["repeat_on"] is True
                assert payload["repeat_seconds"] == 3600
                # Canonical identity and delivery evidence are not smuggled back into
                # editor payload as compatibility markers.
                assert "_publication_id" not in payload
                assert "_content_item_id" not in payload
                assert "result_ids" not in payload
                assert "result_link" not in payload
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_editor_view_fails_closed_for_foreign_owner(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-editor-owner.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, _, publication_id, _ = await _seed_owned_publication(Session)

            async with Session() as session:
                foreign = Client(
                    tg_user_id=70002,
                    username="foreign",
                    full_name="Foreign",
                    is_premium=False,
                    ui_settings={},
                )
                session.add(foreign)
                await session.commit()

                view = await load_owned_publication_editor_view(
                    session,
                    publication_id=publication_id,
                    tg_user_id=70002,
                )
                assert view is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_callback_identity_uses_publication_not_post_task() -> None:
    assert publication_open_callback(42, "2026-08-10") == "cp_open_pub:42:2026-08-10"
    assert publication_edit_callback(42, "2026-08-10") == "cp_edit_pub:42:2026-08-10"
