from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_editor import save_owned_publication_editor_revision


async def _seed_published(Session) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=72001,
            username="owner",
            full_name="Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10072001,
            title="Editable",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Original"}],
                metadata={
                    "source_provenance": {"kind": "test"},
                    "legacy_payload_extra": {
                        "_post_task_id": 999,
                        "autodelete_seconds": 5,
                    },
                },
            ),
            created_by_tg_user_id=72001,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={"autodelete_seconds": 7200},
        )
        publication_id = int(publication.id)
        task_id = int(publication.legacy_post_task_id or 0)
        schedule_id = int(publication.schedule_entry_id or 0)
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [91001, 91002],
            "result_link": "https://t.me/example/91002",
        }
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(publication_id)
        assert publication.status == "published"

        # The save boundary must not need legacy transport after canonical delivery
        # state has been projected.
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(item.id), publication_id, schedule_id, task_id


def test_canonical_edit_save_survives_post_task_retirement(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-editor-save.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, schedule_id, task_id = await _seed_published(Session)

            async with Session() as session:
                result = await save_owned_publication_editor_revision(
                    session,
                    publication_id=publication_id,
                    tg_user_id=72001,
                    expected_content_item_id=item_id,
                    expected_content_revision=1,
                    editor_payload={
                        "type": "text",
                        "text": "Edited canonical text",
                        "autodelete_seconds": 123,
                        "_publication_id": 999,
                        "result_ids": [1, 2, 3],
                    },
                    primary_message_id=91001,
                )
                assert result is not None
                assert result.previous_revision == 1
                assert result.content_revision == 2
                assert result.telegram_message_ids == (91002, 91001)
                assert result.result_link == "https://t.me/example/91001"

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                item = await session.get(ContentItem, item_id)
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert item is not None and item.current_revision == 2
                assert publication is not None and publication.content_revision == 2
                assert schedule is not None and schedule.content_revision == 2
                assert publication.status == "published"
                assert publication.telegram_message_ids == [91002, 91001]
                assert publication.result_link == "https://t.me/example/91001"
                assert publication.meta["runtime_options"]["autodelete_seconds"] == 7200
                assert publication.meta["canonical_edit"]["revision"] == 2

                rows = (
                    await session.execute(
                        select(ContentRevision)
                        .where(ContentRevision.content_item_id == item_id)
                        .order_by(ContentRevision.revision.asc())
                    )
                ).scalars().all()
                assert [row.revision for row in rows] == [1, 2]
                original = PostDocument.from_dict(rows[0].document)
                edited = PostDocument.from_dict(rows[1].document)
                assert original.blocks[0]["text"] == "Original"
                assert edited.blocks[0]["text"] == "Edited canonical text"
                assert edited.metadata["source_provenance"] == {"kind": "test"}
                assert "legacy_payload_extra" not in edited.metadata
                assert rows[1].source == "publication_editor"
                assert rows[1].created_by_tg_user_id == 72001
                assert rows[1].meta["edited_from_revision"] == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_edit_save_fails_closed_for_foreign_or_stale_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'publication-editor-save-guards.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, _, _ = await _seed_published(Session)

            async with Session() as session:
                foreign = Client(
                    tg_user_id=72002,
                    username="foreign",
                    full_name="Foreign",
                    ui_settings={},
                )
                session.add(foreign)
                await session.commit()
                denied = await save_owned_publication_editor_revision(
                    session,
                    publication_id=publication_id,
                    tg_user_id=72002,
                    expected_content_item_id=item_id,
                    expected_content_revision=1,
                    editor_payload={"type": "text", "text": "Foreign overwrite"},
                    primary_message_id=91002,
                )
                assert denied is None

                saved = await save_owned_publication_editor_revision(
                    session,
                    publication_id=publication_id,
                    tg_user_id=72001,
                    expected_content_item_id=item_id,
                    expected_content_revision=1,
                    editor_payload={"type": "text", "text": "First valid edit"},
                    primary_message_id=91002,
                )
                assert saved is not None and saved.content_revision == 2

                stale = await save_owned_publication_editor_revision(
                    session,
                    publication_id=publication_id,
                    tg_user_id=72001,
                    expected_content_item_id=item_id,
                    expected_content_revision=1,
                    editor_payload={"type": "text", "text": "Stale overwrite"},
                    primary_message_id=91002,
                )
                assert stale is None

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                assert item is not None and item.current_revision == 2
                revisions = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == item_id
                        )
                    )
                ).scalars().all()
                assert len(revisions) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
