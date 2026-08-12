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
from app.services.canonical_repeat_time_autodelete import (
    CanonicalRepeatTimeAutodeleteService,
)
from app.services.canonical_repeat_time_pin_autodelete import (
    CanonicalRepeatTimePinAutodeleteService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal(Session, *, seed: int, now: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=246000 + seed,
            username=f"repeat-time-pin-destructive-{seed}",
            full_name=f"Repeat Time Pin Destructive {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100246000 + seed),
            title=f"Repeat Time Pin Destructive {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "repeat time pin destructive",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=3),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "autodelete_seconds": 90,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        finished_at = now - timedelta(seconds=120)
        due_at = finished_at + timedelta(seconds=90)
        publication_meta = dict(publication.meta or {})
        publication_meta[AUTODELETE_RUNTIME_META_KEY] = {
            "deleted": False,
            "effective_seconds": 90,
            "scheduled_at": due_at.isoformat(),
        }
        publication.meta = publication_meta
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
                finished_at=finished_at,
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


class _Provider:
    def __init__(self, session: AsyncSession, *, fail_delete: bool = False) -> None:
        self.session = session
        self.fail_delete = bool(fail_delete)
        self.delete_calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int):
        # Same #281 guarantee: the operation transaction is closed before provider I/O.
        assert not self.session.in_transaction()
        self.delete_calls.append((int(chat_id), int(message_id)))
        if self.fail_delete:
            raise RuntimeError("ambiguous provider failure")
        return True

    async def send_message(self, **kwargs):
        raise AssertionError("test profile does not request delete reports")


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


def test_repeat_time_pin_destructive_admission_is_independent_and_default_off(
    tmp_path,
) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-destructive-off.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=1, now=now)

            async with Session() as session:
                provider = _Provider(session)
                default_off = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=provider,
                ).delete_if_due(publication_id, now=now)
                assert default_off.outcome == "ineligible"
                assert provider.delete_calls == []

            async with Session() as session:
                provider = _Provider(session)
                plain_time = await CanonicalRepeatTimeAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time=True,
                ).delete_if_due(publication_id, now=now)
                assert plain_time.outcome == "ineligible"
                assert provider.delete_calls == []

            assert await _actions(Session, publication_id) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_delete_reuses_exact_281_ledger_and_never_replays(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-destructive-success.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=2, now=now)

            async with Session() as session:
                provider = _Provider(session)
                result = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(publication_id, now=now)
                assert result.outcome == "deleted"
                assert result.deleted_count == 1
                assert len(provider.delete_calls) == 1

            actions = await _actions(Session, publication_id)
            assert len(actions) == 1
            action = actions[0]
            assert action.state == "succeeded"
            assert len(str(action.authority_fingerprint)) == 64
            assert str(action.reservation_token)
            assert str(action.reserved_by_lease_token)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                runtime = dict(
                    dict(publication.meta or {}).get(AUTODELETE_RUNTIME_META_KEY) or {}
                )
                assert runtime.get("deleted") is True

                replay_provider = _Provider(session)
                replay = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(publication_id, now=now + timedelta(seconds=1))
                assert replay.outcome in {"ineligible", "already_deleted"}
                assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_ambiguity_is_permanent_no_replay_barrier(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-destructive-ambiguous.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=3, now=now)

            async with Session() as session:
                provider = _Provider(session, fail_delete=True)
                first = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(publication_id, now=now)
                assert first.outcome == "ambiguous"
                assert len(provider.delete_calls) == 1

            actions = await _actions(Session, publication_id)
            assert len(actions) == 1
            assert actions[0].state == "unknown"

            async with Session() as session:
                replay_provider = _Provider(session)
                replay = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(publication_id, now=now + timedelta(seconds=1))
                assert replay.outcome == "ambiguous"
                assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_lifecycle_drift_blocks_reservation_and_provider(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-destructive-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=4, now=now)

            async with Session() as session:
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                attempt.telegram_message_ids = [999999]
                await session.commit()

            async with Session() as session:
                provider = _Provider(session)
                result = await CanonicalRepeatTimePinAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin=True,
                ).delete_if_due(publication_id, now=now)
                assert result.outcome == "ineligible"
                assert provider.delete_calls == []
            assert await _actions(Session, publication_id) == []
        finally:
            await engine.dispose()

    asyncio.run(run())
