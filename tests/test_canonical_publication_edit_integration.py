from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.telegram_edit_outcome as edit_outcome_module
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_edit import (
    CanonicalPublicationEditCoordinator,
    CanonicalPublicationEditSyncFailed,
)
from app.services.publication_edit_persistence import PublicationEditConflictError
from app.services.telegram_edit_outcome import TelegramEditFailed


class FakeBadRequest(Exception):
    pass


class FakeProvider:
    def __init__(self, *, text_plan=None) -> None:
        self.text_plan = dict(text_plan or {})
        self.text_calls: list[int] = []

    async def edit_message_text(self, **kwargs):
        message_id = int(kwargs["message_id"])
        self.text_calls.append(message_id)
        outcome = self.text_plan.get(message_id)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def edit_message_media(self, **kwargs):
        raise AssertionError("media edit not expected")


async def _seed_published(Session) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=73001,
            username="owner",
            full_name="Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10073001,
            title="Canonical integration",
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
            created_by_tg_user_id=73001,
        )
        from app.services.publication_bridge import LegacyPublicationBridge

        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={"autodelete_seconds": 7200},
        )
        publication_id = int(publication.id)
        schedule_id = int(publication.schedule_entry_id or 0)
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        schedule = await session.get(ScheduleEntry, schedule_id)
        assert task is not None and schedule is not None

        publication.status = "published"
        publication.telegram_message_ids = [93001, 93002]
        schedule.status = "completed"
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(item.id), publication_id, schedule_id, task_id


def test_confirmed_fallback_persists_revision_after_post_task_retirement(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(edit_outcome_module, "TelegramBadRequest", FakeBadRequest)

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-integration.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, schedule_id, task_id = await _seed_published(Session)
            provider = FakeProvider(
                text_plan={
                    93002: FakeBadRequest("stale primary provider secret"),
                    93001: object(),
                }
            )

            result = await CanonicalPublicationEditCoordinator(
                provider=provider,
                session_factory=Session,
            ).edit_text_and_persist(
                publication_id=publication_id,
                tg_user_id=73001,
                expected_revision=1,
                payload={
                    "type": "text",
                    "text": "After",
                    "autodelete_seconds": 7200,
                    "result_ids": [1, 2],
                },
                text="After",
            )

            assert result.message_id == 93001
            assert result.attempted_message_ids == (93002, 93001)
            assert result.telegram_message_ids == (93002, 93001)
            assert result.previous_revision == 1
            assert result.revision == 2
            assert provider.text_calls == [93002, 93001]

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                item = await session.get(ContentItem, item_id)
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                revision = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == item_id,
                            ContentRevision.revision == 2,
                        )
                    )
                ).scalar_one()
                assert item is not None and item.current_revision == 2
                assert publication is not None and publication.content_revision == 2
                assert publication.telegram_message_ids == [93002, 93001]
                assert schedule is not None and schedule.content_revision == 2
                document = PostDocument.from_dict(revision.document)
                assert document.blocks[0]["text"] == "After"
                extras = dict(document.metadata.get("legacy_payload_extra") or {})
                assert "autodelete_seconds" not in extras
                assert "result_ids" not in extras
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_failure_does_not_create_canonical_revision(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-provider-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, _, _ = await _seed_published(Session)
            provider = FakeProvider(
                text_plan={93002: RuntimeError("credential-like provider detail")}
            )

            with pytest.raises(TelegramEditFailed):
                await CanonicalPublicationEditCoordinator(
                    provider=provider,
                    session_factory=Session,
                ).edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=73001,
                    expected_revision=1,
                    payload={"type": "text", "text": "Never persisted"},
                    text="Never persisted",
                )
            assert provider.text_calls == [93002]

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                count = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == item_id
                        )
                    )
                ).scalars().all()
                assert item is not None and item.current_revision == 1
                assert len(count) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_preflight_blocks_provider_side_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-stale-preflight.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            item_id, publication_id, schedule_id, _ = await _seed_published(Session)

            async with Session() as session:
                item = await session.get(ContentItem, item_id)
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert item is not None and publication is not None and schedule is not None
                session.add(
                    ContentRevision(
                        content_item_id=item_id,
                        revision=2,
                        document=PostDocument(
                            blocks=[{"id": "b1", "type": "text", "text": "Concurrent"}]
                        ).to_dict(),
                        source="editor",
                        created_by_tg_user_id=73001,
                        meta={},
                    )
                )
                item.current_revision = 2
                publication.content_revision = 2
                schedule.content_revision = 2
                await session.commit()

            provider = FakeProvider(text_plan={93002: object()})
            with pytest.raises(PublicationEditConflictError):
                await CanonicalPublicationEditCoordinator(
                    provider=provider,
                    session_factory=Session,
                ).edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=73001,
                    expected_revision=1,
                    payload={"type": "text", "text": "Stale"},
                    text="Stale",
                )
            assert provider.text_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_provider_persistence_conflict_is_distinguished(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-edit-post-provider-race.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, _, _ = await _seed_published(Session)
            provider = FakeProvider(text_plan={93002: object()})

            from app.services import canonical_publication_edit as coordinator_module

            async def conflict_after_provider(self, **kwargs):
                raise PublicationEditConflictError("simulated post-provider race")

            monkeypatch.setattr(
                coordinator_module.PublicationEditPersistenceService,
                "persist_success",
                conflict_after_provider,
            )

            with pytest.raises(CanonicalPublicationEditSyncFailed) as captured:
                await CanonicalPublicationEditCoordinator(
                    provider=provider,
                    session_factory=Session,
                ).edit_text_and_persist(
                    publication_id=publication_id,
                    tg_user_id=73001,
                    expected_revision=1,
                    payload={"type": "text", "text": "Provider already changed"},
                    text="Provider already changed",
                )
            assert captured.value.conflict is True
            assert str(captured.value) == "canonical edit sync failed"
            assert provider.text_calls == [93002]
        finally:
            await engine.dispose()

    asyncio.run(run())
