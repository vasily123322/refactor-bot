from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_views_lifecycle_authority import (
    CanonicalRepeatViewsLifecycleAuthorityService,
)
from app.services.publication_autodelete_views_composed import (
    PublicationAutodeleteViewsComposedService,
)
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_terminal(Session, *, now: datetime, seed: int) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=234000 + seed,
            username=f"views-pin-forward-lifecycle-{seed}",
            full_name=f"Views Pin Forward Lifecycle {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100234000 + seed),
            title=f"Views Pin Forward Lifecycle Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100334000 + seed),
            title=f"Views Pin Forward Lifecycle A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100434000 + seed),
            title=f"Views Pin Forward Lifecycle B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "combined lifecycle"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "forward_to": [int(target_b.id), int(target_a.id)],
                "autodelete_views": 43,
                "autodelete_report": True,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=43,
            now=now - timedelta(minutes=1),
        )
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [9700 + seed]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[9700 + seed],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(seconds=30),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


class _Views:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return 99


class _Delete:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.calls.append((int(chat_id), int(message_id)))


def test_combined_lifecycle_requires_all_three_explicit_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-lifecycle-facts.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal(Session, now=now, seed=1)

            cases = (
                {},
                {"allow_pin": True},
                {"allow_forward": True},
                {"allow_pin": True, "allow_forward": True},
                {"allow_pin_forward": True},
                {"allow_pin": True, "allow_pin_forward": True},
                {"allow_forward": True, "allow_pin_forward": True},
            )
            for kwargs in cases:
                async with Session() as session:
                    assert (
                        await CanonicalRepeatViewsLifecycleAuthorityService(
                            session
                        ).lock_and_prove(publication_id, **kwargs)
                        is None
                    )

            async with Session() as session:
                proof = await CanonicalRepeatViewsLifecycleAuthorityService(
                    session
                ).lock_and_prove(
                    publication_id,
                    allow_pin=True,
                    allow_forward=True,
                    allow_pin_forward=True,
                )
                assert proof is not None
                assert proof.threshold == 43
                assert proof.telegram_message_ids == (9701,)
                assert proof.runtime_options["pin_on"] is True
                assert proof.runtime_options["forward_to"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_composed_destructive_service_cannot_infer_combined_lifecycle(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-lifecycle-destructive-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal(Session, now=now, seed=2)
            views = _Views()
            delete = _Delete()

            async with Session() as session:
                result = await PublicationAutodeleteViewsComposedService(
                    session,
                    view_source=views,
                    delete_provider=delete,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                    allow_repeat_views_forward=True,
                ).evaluate_and_delete(publication_id, now=now)
                assert result.outcome == "ineligible"
            assert views.calls == []
            assert delete.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
