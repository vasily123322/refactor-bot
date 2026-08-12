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
from app.domain.publishing.models import PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_time_forward_autodelete import (
    CanonicalRepeatTimeForwardAutodeleteService,
)
from app.services.canonical_repeat_time_pin_autodelete import (
    CanonicalRepeatTimePinAutodeleteService,
)
from app.services.canonical_repeat_time_pin_forward_autodelete import (
    CanonicalRepeatTimePinForwardAutodeleteService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal(Session, seed: int, now: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=258000 + seed,
            username=f"combined-delete-{seed}",
            full_name="Combined Delete",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100258000 + seed), title="Source", owner_id=int(owner.id), is_active=True
        )
        target = Channel(
            tg_chat_id=-(100358000 + seed), title="Target", owner_id=int(owner.id), is_active=True
        )
        session.add_all([source, target])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(blocks=[{"id": "b1", "type": "text", "text": "combined"}]),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=3),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "pin_on": True,
                "forward_to": [int(target.id)],
                "autodelete_seconds": 90,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        finished_at = now - timedelta(seconds=120)
        meta = dict(publication.meta or {})
        meta[AUTODELETE_RUNTIME_META_KEY] = {
            "deleted": False,
            "effective_seconds": 90,
            "scheduled_at": (finished_at + timedelta(seconds=90)).isoformat(),
        }
        publication.meta = meta
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [10000 + seed]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[10000 + seed],
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
        self.calls = 0

    async def delete_message(self, **kwargs) -> None:
        assert not self.session.in_transaction()
        self.calls += 1
        if self.fail:
            raise RuntimeError("ambiguous delete")

    async def send_message(self, **kwargs) -> None:
        raise AssertionError("report not requested")


async def _actions(Session, publication_id: int):
    async with Session() as session:
        return (
            await session.execute(
                select(PublicationAutodeleteAction).where(
                    PublicationAutodeleteAction.publication_id == publication_id
                )
            )
        ).scalars().all()


def test_combined_delete_is_not_inferred_from_narrower_adapters(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-delete-off.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, 1, now)
            for service_type, keyword in (
                (CanonicalRepeatTimePinAutodeleteService, "allow_repeat_time_pin"),
                (
                    CanonicalRepeatTimeForwardAutodeleteService,
                    "allow_repeat_time_forward",
                ),
            ):
                async with Session() as session:
                    provider = _Provider(session)
                    result = await service_type(
                        session,
                        provider=provider,
                        **{keyword: True},
                    ).delete_if_due(publication_id, now=now)
                    assert result.outcome == "ineligible"
                    assert provider.calls == 0
            assert await _actions(Session, publication_id) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_combined_delete_success_and_ambiguity_keep_exact_no_replay(tmp_path) -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-delete-ledger.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            success_id = await _seed_terminal(Session, 2, now)
            async with Session() as session:
                provider = _Provider(session)
                result = await CanonicalRepeatTimePinForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin_forward=True,
                ).delete_if_due(success_id, now=now)
                assert result.outcome == "deleted"
                assert provider.calls == 1
            success_actions = await _actions(Session, success_id)
            assert len(success_actions) == 1
            assert success_actions[0].state == "succeeded"

            ambiguous_id = await _seed_terminal(Session, 3, now)
            async with Session() as session:
                provider = _Provider(session, fail=True)
                result = await CanonicalRepeatTimePinForwardAutodeleteService(
                    session,
                    provider=provider,
                    allow_repeat_time_pin_forward=True,
                ).delete_if_due(ambiguous_id, now=now)
                assert result.outcome == "ambiguous"
                assert provider.calls == 1
            ambiguous_actions = await _actions(Session, ambiguous_id)
            assert len(ambiguous_actions) == 1
            assert ambiguous_actions[0].state == "unknown"

            async with Session() as session:
                replay_provider = _Provider(session)
                replay = await CanonicalRepeatTimePinForwardAutodeleteService(
                    session,
                    provider=replay_provider,
                    allow_repeat_time_pin_forward=True,
                ).delete_if_due(ambiguous_id, now=now + timedelta(seconds=1))
                assert replay.outcome == "ambiguous"
                assert replay_provider.calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
