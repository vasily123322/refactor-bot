from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.canonical_migration_control_middleware import (
    CanonicalMigrationControlMiddleware,
)
from app.core.callbacks import CB
from app.core.db import Base
from app.core.settings_channel_access import _resolve_repeat_group_target
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry


class _State:
    def __init__(self, data: dict) -> None:
        self._data = data

    async def get_data(self) -> dict:
        return dict(self._data)


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=990001)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", *, show_alert: bool = False, **kwargs):
        self.answers.append((str(text), bool(show_alert)))


async def _seed(Session, *, group_id: int = 777001):
    async with Session() as session:
        owner = Client(
            tg_user_id=990001,
            username="migration-control",
            full_name="Migration Control",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100990001,
            title="Migration Control",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()

        # Root compatibility task is intentionally absent: canonical handoff retired it.
        schedule = ScheduleEntry(
            content_item_id=1,
            content_revision=1,
            channel_id=int(channel.id),
            scheduled_at=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            timezone="UTC",
            status="completed",
            repeat_rule={"enabled": True, "seconds": 60},
            meta={"repeat_group_id": group_id},
        )
        session.add(schedule)
        await session.flush()

        linked_task = PostTask(
            channel_id=int(channel.id),
            status="pending",
            scheduled_at=datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc),
            payload={"type": "text", "text": "linked"},
        )
        legacy_task = PostTask(
            channel_id=int(channel.id),
            status="pending",
            scheduled_at=datetime(2026, 8, 13, 12, 6, tzinfo=timezone.utc),
            payload={"type": "text", "text": "legacy"},
        )
        session.add_all([linked_task, legacy_task])
        await session.flush()
        publication = Publication(
            schedule_entry_id=int(schedule.id),
            content_item_id=1,
            content_revision=1,
            channel_id=int(channel.id),
            status="queued",
            legacy_post_task_id=int(linked_task.id),
            attempt_count=0,
            meta={},
        )
        session.add(publication)
        await session.commit()
        return int(channel.id), int(linked_task.id), int(legacy_task.id)


def test_repeat_group_owner_resolution_survives_retired_root_posttask(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-owner-resolution.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, _, _ = await _seed(Session)
            async with Session() as session:
                resolved = await _resolve_repeat_group_target(session, 777001)
                assert resolved == channel_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_group_owner_resolution_fails_closed_on_cross_channel_ambiguity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-owner-ambiguity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            await _seed(Session, group_id=777002)
            async with Session() as session:
                owner = Client(
                    tg_user_id=990002,
                    username="other",
                    full_name="Other",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-100990002,
                    title="Other",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add(channel)
                await session.flush()
                session.add(
                    ScheduleEntry(
                        content_item_id=2,
                        content_revision=1,
                        channel_id=int(channel.id),
                        scheduled_at=datetime(2026, 8, 13, 12, 10, tzinfo=timezone.utc),
                        timezone="UTC",
                        status="completed",
                        repeat_rule={"enabled": True, "seconds": 60},
                        meta={"repeat_group_id": 777002},
                    )
                )
                await session.commit()
            async with Session() as session:
                assert await _resolve_repeat_group_target(session, 777002) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_autodelete_editor_is_blocked_but_unlinked_legacy_passes(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-autodel-guard.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, linked_id, legacy_id = await _seed(Session, group_id=777003)
            middleware = CanonicalMigrationControlMiddleware(session_factory=Session)
            calls: list[int] = []

            async def handler(event, data):
                calls.append(1)
                return "handled"

            linked = _Callback(CB.EDIT_AUTODEL)
            result = await middleware(
                handler,
                linked,  # type: ignore[arg-type]
                {"state": _State({"return_to_notice": {"post_id": linked_id}})},
            )
            assert result is None
            assert calls == []
            assert linked.answers and linked.answers[-1][1] is True
            assert "не выполнено" in linked.answers[-1][0]

            legacy = _Callback(CB.EDIT_AUTODEL)
            result = await middleware(
                handler,
                legacy,  # type: ignore[arg-type]
                {"state": _State({"return_to_notice": {"post_id": legacy_id}})},
            )
            assert result == "handled"
            assert calls == [1]
            assert legacy.answers == []

            unrelated = _Callback("some_other_callback")
            result = await middleware(
                handler,
                unrelated,  # type: ignore[arg-type]
                {"state": _State({"return_to_notice": {"post_id": linked_id}})},
            )
            assert result == "handled"
            assert calls == [1, 1]
        finally:
            await engine.dispose()

    asyncio.run(run())
