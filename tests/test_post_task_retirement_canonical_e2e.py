from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_edit import CanonicalPublicationEditCoordinator
from app.services.post_task_retention import PostTaskRetentionService
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.publication_autodelete import PublicationAutodeleteWorker
from app.workers.publication_autodelete_views import PublicationAutodeleteViewsWorker


class FakeTelegramProvider:
    def __init__(self) -> None:
        self.edit_calls: list[dict] = []
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[dict] = []

    async def edit_message_text(self, **kwargs) -> None:
        self.edit_calls.append(dict(kwargs))

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.delete_calls.append((int(chat_id), int(message_id)))
        return True

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ):
        self.report_calls.append(
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "disable_web_page_preview": bool(disable_web_page_preview),
            }
        )
        return True


class FakeViewSource:
    def __init__(self, values: dict[int, int | None]) -> None:
        self.values = dict(values)
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: str | int, message_id: int) -> int | None:
        self.calls.append((int(target), int(message_id)))
        return self.values.get(int(message_id))


async def _seed_pending_published(
    Session,
    *,
    seed_id: int,
    now: datetime,
    mode: str,
) -> tuple[int, int, int, int, str]:
    async with Session() as session:
        owner_tg_user_id = 97000 + seed_id
        owner = Client(
            tg_user_id=owner_tg_user_id,
            username=f"retirement-e2e-owner-{seed_id}",
            full_name=f"Retirement E2E Owner {seed_id}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10097000 + seed_id),
            title=f"Retirement E2E {seed_id}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        if mode == "time":
            runtime_options: dict[str, object] = {
                "autodelete_seconds": 3600,
                "autodelete_report": True,
            }
        elif mode == "views":
            runtime_options = {
                "autodelete_views": 100,
                "autodelete_report": True,
            }
        else:
            raise AssertionError("unsupported mode")

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Retirement E2E {mode}",
                    }
                ]
            ),
            created_by_tg_user_id=owner_tg_user_id,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(days=120),
            runtime_options=runtime_options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None

        message_id = 98000 + seed_id
        result_link = f"https://t.me/c/{97000 + seed_id}/{message_id}"
        payload = {
            **dict(task.payload or {}),
            **runtime_options,
            "result_ids": [message_id],
            "result_link": result_link,
        }
        due = now - timedelta(minutes=1)
        if mode == "time":
            payload.update(
                {
                    "autodelete_effective_seconds": 3600,
                    "autodelete_at": due.isoformat(),
                    "autodeleted": False,
                }
            )
        task.payload = payload
        task.status = "done"
        await session.commit()

        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == int(publication.id),
                    PublicationAttempt.attempt == int(publication.attempt_count),
                )
            )
        ).scalar_one()
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id or 0))
        assert schedule is not None
        attempt.finished_at = now - timedelta(days=120)
        publication.result_link = result_link
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": dict(runtime_options),
        }
        if mode == "time":
            publication.meta = {
                **dict(publication.meta or {}),
                AUTODELETE_RUNTIME_META_KEY: {
                    "effective_seconds": 3600,
                    "scheduled_at": due.isoformat(),
                    "deleted": False,
                },
            }
        else:
            await PublicationAutodeleteViewStateService(session).sync_intent(
                publication_id=int(publication.id),
                threshold=100,
                now=now,
            )
        await session.commit()
        return (
            int(task.id),
            int(publication.id),
            int(channel.tg_chat_id),
            owner_tg_user_id,
            result_link,
        )


async def _retire_pending_transport(Session, *, publication_id: int, task_id: int, now: datetime) -> None:
    async with Session() as session:
        tick = await PostTaskRetentionService(
            session,
            retention_days=90,
            batch_size=10,
            retire_successful=True,
            retire_successful_pending_autodelete=True,
        ).run_once(now=now)
    assert tick.deleted == 1
    assert tick.failures == 0

    async with Session() as session:
        assert await session.get(PostTask, task_id) is None
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        assert publication.legacy_post_task_id is None
        assert publication.meta["legacy_transport_retention"]["retired"] is True


def test_retired_time_transport_remains_editable_then_canonical_worker_deletes(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'retired-time-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            task_id, publication_id, chat_id, owner_tg_user_id, result_link = (
                await _seed_pending_published(
                    Session,
                    seed_id=1,
                    now=now,
                    mode="time",
                )
            )
            await _retire_pending_transport(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=now,
            )

            provider = FakeTelegramProvider()
            edit = await CanonicalPublicationEditCoordinator(
                provider=provider,
                session_factory=Session,
            ).edit_text_and_persist(
                publication_id=publication_id,
                tg_user_id=owner_tg_user_id,
                expected_revision=1,
                payload={
                    "type": "text",
                    "text": "Edited after time transport retirement",
                    "autodelete_seconds": 3600,
                    "autodelete_report": True,
                },
                text="Edited after time transport retirement",
            )
            assert edit.previous_revision == 1
            assert edit.revision == 2
            assert len(provider.edit_calls) == 1
            assert provider.edit_calls[0]["chat_id"] == chat_id
            assert provider.edit_calls[0]["message_id"] == 98001

            worker = PublicationAutodeleteWorker(
                provider=provider,
                session_factory=Session,
                batch_size=10,
            )
            tick = await worker.run_once()
            assert tick.selected == 1
            assert tick.deleted == 1
            assert tick.failures == 0
            assert provider.delete_calls == [(chat_id, 98001)]
            assert provider.report_calls == [
                {
                    "chat_id": owner_tg_user_id,
                    "text": f"🗑️ Пост удалён по таймеру\n{result_link}",
                    "disable_web_page_preview": True,
                }
            ]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0) if publication else 0,
                )
                assert publication is not None and schedule is not None
                assert publication.legacy_post_task_id is None
                assert publication.content_revision == 2
                assert schedule.content_revision == 2
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
                assert publication.meta["legacy_transport_retention"]["retired"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retired_views_transport_remains_editable_then_canonical_worker_reports(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'retired-views-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            task_id, publication_id, chat_id, owner_tg_user_id, result_link = (
                await _seed_pending_published(
                    Session,
                    seed_id=2,
                    now=now,
                    mode="views",
                )
            )
            await _retire_pending_transport(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=now,
            )

            provider = FakeTelegramProvider()
            edit = await CanonicalPublicationEditCoordinator(
                provider=provider,
                session_factory=Session,
            ).edit_text_and_persist(
                publication_id=publication_id,
                tg_user_id=owner_tg_user_id,
                expected_revision=1,
                payload={
                    "type": "text",
                    "text": "Edited after views transport retirement",
                    "autodelete_views": 100,
                    "autodelete_report": True,
                },
                text="Edited after views transport retirement",
            )
            assert edit.previous_revision == 1
            assert edit.revision == 2
            assert len(provider.edit_calls) == 1
            assert provider.edit_calls[0]["chat_id"] == chat_id
            assert provider.edit_calls[0]["message_id"] == 98002

            views = FakeViewSource({98002: 150})
            worker = PublicationAutodeleteViewsWorker(
                view_source=views,
                delete_provider=provider,
                session_factory=Session,
                batch_size=10,
            )
            tick = await worker.run_once(now=now + timedelta(seconds=1))
            assert tick.selected == 1
            assert tick.deleted == 1
            assert tick.failures == 0
            assert views.calls == [(chat_id, 98002)]
            assert provider.delete_calls == [(chat_id, 98002)]
            assert provider.report_calls == [
                {
                    "chat_id": owner_tg_user_id,
                    "text": f"🗑️ Пост удалён по просмотрам\n{result_link}",
                    "disable_web_page_preview": True,
                }
            ]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0) if publication else 0,
                )
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None and schedule is not None
                assert publication.legacy_post_task_id is None
                assert publication.content_revision == 2
                assert schedule.content_revision == 2
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["mode"] == "views"
                assert publication.meta["legacy_transport_retention"]["retired"] is True
                assert state is None
        finally:
            await engine.dispose()

    asyncio.run(run())
