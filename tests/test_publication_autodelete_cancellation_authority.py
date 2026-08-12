from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteAction
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete import PublicationAutodeleteService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed(Session, *, due_at: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=88901,
            username="autodelete-cancel-owner",
            full_name="Autodelete Cancel Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10088901,
            title="Autodelete Cancel",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Cancel delete"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={"autodelete_seconds": 3600},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert task is not None and schedule is not None
        task.status = "done"
        publication.status = "published"
        publication.telegram_message_ids = [8890101]
        schedule.status = "completed"
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": {"autodelete_seconds": 3600},
            AUTODELETE_RUNTIME_META_KEY: {
                "scheduled_at": due_at.isoformat(),
                "effective_seconds": 3600,
                "deleted": False,
            },
        }
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_provider_cancellation_survives_lease_expiry_as_no_replay_barrier(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'autodelete-cancel-authority.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
            publication_id = await _seed(
                Session,
                due_at=now - timedelta(minutes=1),
            )

            class CancellingProvider:
                calls: list[tuple[int, int]] = []

                async def delete_message(self, *, chat_id: int, message_id: int):
                    self.calls.append((int(chat_id), int(message_id)))
                    raise asyncio.CancelledError()

            first_provider = CancellingProvider()
            async with Session() as session:
                with pytest.raises(asyncio.CancelledError):
                    await PublicationAutodeleteService(
                        session,
                        provider=first_provider,
                    ).delete_if_due(publication_id, now=now)
            assert first_provider.calls == [(-10088901, 8890101)]

            async with Session() as session:
                action = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id == publication_id,
                            PublicationAutodeleteAction.telegram_message_id == 8890101,
                        )
                    )
                ).scalar_one()
                assert action.state in {"reserved", "unknown"}
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is False

            class ReplayProvider:
                calls: list[tuple[int, int]] = []

                async def delete_message(self, *, chat_id: int, message_id: int):
                    self.calls.append((int(chat_id), int(message_id)))
                    return True

            replay_provider = ReplayProvider()
            async with Session() as session:
                replay = await PublicationAutodeleteService(
                    session,
                    provider=replay_provider,
                ).delete_if_due(
                    publication_id,
                    now=now + timedelta(seconds=181),
                )
            assert replay.outcome == "ambiguous"
            assert replay_provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
