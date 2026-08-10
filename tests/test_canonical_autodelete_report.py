from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete import (
    PublicationAutodeleteService,
    PublicationAutodeleteSyncConflict,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


class DeleteUnavailable(Exception):
    pass


class ReportProvider:
    def __init__(self) -> None:
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[dict[str, object]] = []
        self.report_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.report_state_probe = None
        self.after_delete = None

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.delete_calls.append((int(chat_id), int(message_id)))
        if self.after_delete is not None:
            await self.after_delete()
        if self.delete_error is not None:
            raise self.delete_error
        return True

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ):
        if self.report_state_probe is not None:
            self.report_state_probe()
        self.report_calls.append(
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "disable_web_page_preview": bool(disable_web_page_preview),
            }
        )
        if self.report_error is not None:
            raise self.report_error
        return True


async def _seed(Session, *, now: datetime) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=881001,
            username="report-owner",
            full_name="Report Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100881001,
            title="Canonical report",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Delete with report"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={
                "autodelete_seconds": 3600,
                "autodelete_report": True,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id or 0))
        assert task is not None and schedule is not None

        task.status = "done"
        publication.status = "published"
        schedule.status = "completed"
        publication.telegram_message_ids = [991001]
        publication.result_link = "https://t.me/c/881001/991001"
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": {
                "autodelete_seconds": 3600,
                "autodelete_report": True,
            },
            AUTODELETE_RUNTIME_META_KEY: {
                "effective_seconds": 3600,
                "scheduled_at": (now - timedelta(minutes=1)).isoformat(),
                "deleted": False,
            },
        }
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id), int(channel.id), int(owner.tg_user_id)


def test_report_is_sent_after_durable_canonical_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-report.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, owner_tg_id = await _seed(Session, now=now)
            provider = ReportProvider()

            async with Session() as session:
                def probe() -> None:
                    assert not session.in_transaction()

                provider.report_state_probe = probe
                result = await PublicationAutodeleteService(
                    session,
                    provider=provider,
                    allow_report=True,
                ).delete_if_due(publication_id, now=now)

            assert result.outcome == "deleted"
            assert provider.delete_calls == [(-100881001, 991001)]
            assert provider.report_calls == [
                {
                    "chat_id": owner_tg_id,
                    "text": "🗑️ Пост удалён по таймеру\nhttps://t.me/c/881001/991001",
                    "disable_web_page_preview": True,
                }
            ]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                runtime = publication.meta[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["deleted"] is True
                assert runtime["deleted_at"] == now.isoformat()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_failure_does_not_turn_resolved_delete_into_retry(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-report-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(Session, now=now)
            provider = ReportProvider()
            provider.report_error = RuntimeError("provider secret must not affect state")

            async with Session() as session:
                result = await PublicationAutodeleteService(
                    session,
                    provider=provider,
                    allow_report=True,
                ).delete_if_due(publication_id, now=now)

            assert result.outcome == "deleted"
            assert len(provider.report_calls) == 1
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unavailable_only_resolution_does_not_send_false_report(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-report-unavailable.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(Session, now=now)
            provider = ReportProvider()
            provider.delete_error = DeleteUnavailable("message to delete not found")

            async with Session() as session:
                result = await PublicationAutodeleteService(
                    session,
                    provider=provider,
                    allow_report=True,
                ).delete_if_due(publication_id, now=now)

            assert result.outcome == "deleted"
            assert result.deleted_count == 0
            assert result.unavailable_count == 1
            assert provider.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_toggle_after_provider_delete_is_sync_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-report-race.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
            publication_id, _, _ = await _seed(Session, now=now)
            provider = ReportProvider()

            async def mutate_report() -> None:
                provider.after_delete = None
                async with Session() as mutation_session:
                    publication = await mutation_session.get(Publication, publication_id)
                    assert publication is not None
                    meta = dict(publication.meta or {})
                    options = dict(meta.get("runtime_options") or {})
                    options["autodelete_report"] = False
                    meta["runtime_options"] = options
                    publication.meta = meta
                    await mutation_session.commit()

            provider.after_delete = mutate_report
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteSyncConflict):
                    await PublicationAutodeleteService(
                        session,
                        provider=provider,
                        allow_report=True,
                    ).delete_if_due(publication_id, now=now)

            assert provider.report_calls == []
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is False
        finally:
            await engine.dispose()

    asyncio.run(run())