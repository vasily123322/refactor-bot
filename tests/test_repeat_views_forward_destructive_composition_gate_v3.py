from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_autodelete_views_action_ledger import (
    AUTODELETE_VIEWS_ACTIONS_META_KEY,
)
from app.services.publication_autodelete_views_composed import (
    PublicationAutodeleteViewsComposedService,
)
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal(
    Session,
    *,
    seed: int,
    now: datetime,
    pin: bool = False,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=230000 + seed,
            username=f"views-forward-destructive-{seed}",
            full_name=f"Views Forward Destructive {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100230000 + seed),
            title=f"Views Forward Destructive Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100330000 + seed),
            title=f"Views Forward Destructive A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100430000 + seed),
            title=f"Views Forward Destructive B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        options: dict[str, object] = {
            "forward_to": [int(target_b.id), int(target_a.id)],
            "autodelete_views": 31,
        }
        if pin:
            options["pin_on"] = True
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "views forward destructive"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=31,
            now=now - timedelta(minutes=1),
        )
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [9400 + seed]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[9400 + seed],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(seconds=30),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _acquire(Session, publication_id: int, holder: str):
    async with Session() as session:
        handle = await PublicationAutodeleteLeaseService(session).acquire(
            publication_id=publication_id,
            holder=holder,
            ttl_seconds=180,
            allow_linked=True,
        )
        assert handle is not None
        return handle


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


def test_views_forward_destructive_gate_does_not_inherit_plain_repeat_views_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-destructive-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal(Session, seed=1, now=now)
            handle = await _acquire(Session, publication_id, "views-forward-closed")
            views = _Views()
            delete = _Delete()
            async with Session() as session:
                result = await PublicationAutodeleteViewsComposedService(
                    session,
                    view_source=views,
                    delete_provider=delete,
                    allow_repeat_views=True,
                    lease=handle,
                ).evaluate_and_delete(publication_id, now=now)
                assert result.outcome == "ineligible"
            assert views.calls == []
            assert delete.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_forward_explicit_destructive_fact_reuses_exact_v3_reservation_boundary(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-destructive-open.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal(Session, seed=2, now=now)
            handle = await _acquire(Session, publication_id, "views-forward-open")
            views = _Views()
            delete = _Delete()

            async with Session() as session:
                result = await PublicationAutodeleteViewsComposedService(
                    session,
                    view_source=views,
                    delete_provider=delete,
                    allow_repeat_views=True,
                    allow_repeat_views_forward=True,
                    lease=handle,
                ).evaluate_and_delete(publication_id, now=now)
                assert result.outcome == "deleted"
                assert result.deleted_count == 1
            assert len(views.calls) == 1
            assert len(delete.calls) == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None
                assert state is None
                meta = dict(publication.meta or {})
                runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict) and runtime.get("deleted") is True
                ledger = meta.get(AUTODELETE_VIEWS_ACTIONS_META_KEY)
                assert isinstance(ledger, dict)
                actions = ledger.get("actions")
                assert isinstance(actions, dict) and len(actions) == 1
                action = next(iter(actions.values()))
                assert action["state"] == "succeeded"
                assert action["autodelete_lease_token"] == handle.lease_token
                assert action["autodelete_lease_holder"] == handle.holder
                assert str(action["reservation_token"])
                assert action["authority_fingerprint"] == ledger["authority_fingerprint"]

            views_before = list(views.calls)
            async with Session() as session:
                replay = await PublicationAutodeleteViewsComposedService(
                    session,
                    view_source=views,
                    delete_provider=delete,
                    allow_repeat_views=True,
                    allow_repeat_views_forward=True,
                    lease=handle,
                ).evaluate_and_delete(publication_id, now=now + timedelta(seconds=1))
                assert replay.outcome == "already_deleted"
            assert views.calls == views_before
            assert len(delete.calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_pin_forward_destructive_authority_does_not_compose_independent_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-pin-forward-destructive-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal(Session, seed=3, now=now, pin=True)
            handle = await _acquire(Session, publication_id, "views-pin-forward-closed")
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
                    lease=handle,
                ).evaluate_and_delete(publication_id, now=now)
                assert result.outcome == "ineligible"
            assert views.calls == []
            assert delete.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
