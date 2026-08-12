from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteAction
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_time_autodelete import CanonicalRepeatTimeAutodeleteService
from app.services.canonical_repeat_time_forward_autodelete import (
    CanonicalRepeatTimeForwardAutodeleteService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal(Session, *, seed: int, now: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=252000 + seed,
            username=f"repeat-time-forward-destructive-{seed}",
            full_name=f"Repeat Time Forward Destructive {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100252000 + seed),
            title=f"Repeat Time Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100352000 + seed),
            title=f"Repeat Time Forward Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time forward delete"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=3),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "forward_to": [int(target.id)],
                "autodelete_seconds": 90,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        finished_at = now - timedelta(seconds=120)
        due_at = finished_at + timedelta(seconds=90)
        meta = dict(publication.meta or {})
        meta[AUTODELETE_RUNTIME_META_KEY] = {
            "deleted": False,
            "effective_seconds": 90,
            "scheduled_at": due_at.isoformat(),
        }
        publication.meta = meta
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
                finished_at=finished_at,
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


class _Provider:
    def __init__(self, session: AsyncSession, *, fail: bool = False) -> None:
        self.session = session
        self.fail = bool(fail)
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int):
        assert not self.session.in_transaction()
        self.calls.append((int(chat_id), int(message_id)))
        if self.fail:
            raise RuntimeError("ambiguous provider failure")
        return True

    async def send_message(self, **kwargs):
        raise AssertionError("test profile does not request report")


async def _actions(Session, publication_id: int):
    async with Session() as session:
        return list(
            (
                await session.execute(
                    select(PublicationAutodeleteAction).where(
                        PublicationAutodeleteAction.publication_id == publication_id
                    )
                )
            ).scalars().all()
        )


def test_time_forward_destructive_admission_is_default_off_and_not_plain_time(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'time-forward-delete-off.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=1, now=now)

            async with Session() as session:
                provider = _Provider(session)
                result = await CanonicalRepeatTimeForwardAutodeleteService(
                    session, provider=provider
                ).delete_if_due(publication_id, now=now)
                assert result.outcome == "ineligible"
                assert provider.calls == []

            async with Session() as session:
                provider = _Provider(session)
                result = await CanonicalRepeatTimeAutodeleteService(
                    session, provider=provider, allow_repeat_time=True
                ).delete_if_due(publication_id, now=now)
                assert result.outcome == "ineligible"
                assert provider.calls == []
            assert await _actions(Session, publication_id) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_time_forward_delete_uses_same_ledger_and_success_never_replays(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'time-forward-delete-success.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=2, now=now)

            async with Session() as session:
                provider = _Provider(session)
                result = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(publication_id, now=now)
                assert result.outcome == "deleted"
                assert result.deleted_count == 1
                assert len(provider.calls) == 1

            actions = await _actions(Session, publication_id)
            assert len(actions) == 1
            assert actions[0].state == "succeeded"
            assert len(str(actions[0].authority_fingerprint)) == 64

            async with Session() as session:
                replay_provider = _Provider(session)
                replay = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(publication_id, now=now + timedelta(seconds=1))
                assert replay.outcome in {"ineligible", "already_deleted"}
                assert replay_provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_time_forward_ambiguous_delete_persists_unknown_and_blocks_replay(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'time-forward-delete-ambiguous.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=3, now=now)

            async with Session() as session:
                provider = _Provider(session, fail=True)
                first = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(publication_id, now=now)
                assert first.outcome == "ambiguous"
                assert len(provider.calls) == 1

            actions = await _actions(Session, publication_id)
            assert len(actions) == 1
            assert actions[0].state == "unknown"

            async with Session() as session:
                replay_provider = _Provider(session)
                replay = await CanonicalRepeatTimeForwardAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_forward=True,
                ).delete_if_due(publication_id, now=now + timedelta(seconds=1))
                assert replay.outcome == "ambiguous"
                assert replay_provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
