from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_edit import CanonicalPublicationEditCoordinator
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_edit_persistence import PublicationEditConflictError


def test_inconsistent_linked_transport_is_rejected_before_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-transport-preflight.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                owner = Client(
                    tg_user_id=71401,
                    username="transport-owner",
                    full_name="Transport Owner",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-10071401,
                    title="Transport preflight",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add(channel)
                await session.commit()
                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Before"}]
                    ),
                    created_by_tg_user_id=71401,
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
                )
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                task = await session.get(
                    PostTask, int(publication.legacy_post_task_id or 0)
                )
                assert schedule is not None and task is not None
                publication.status = "published"
                schedule.status = "completed"
                publication.telegram_message_ids = [85101]
                # Deliberately leave the linked transport pending. This is corrupted
                # cross-lifecycle state and must be rejected before Telegram is touched.
                assert task.status == "pending"
                await session.commit()
                publication_id = int(publication.id)

            coordinator = CanonicalPublicationEditCoordinator(
                provider=object(),  # type: ignore[arg-type] - provider must stay unused
                session_factory=Session,
            )
            with pytest.raises(
                PublicationEditConflictError,
                match="legacy transport is not consistently published",
            ):
                await coordinator.edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=71401,
                    expected_revision=1,
                    payload={"type": "text", "text": "Must not reach provider"},
                    text="Must not reach provider",
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
