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
from app.services.canonical_repeat_time_lifecycle_authority import (
    CanonicalRepeatTimeLifecycleAuthorityService,
)
from app.services.canonical_repeat_time_pin_lifecycle_authority import (
    CanonicalRepeatTimePinLifecycleAuthorityService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal(
    Session,
    *,
    seed: int,
    options: dict[str, object] | None = None,
    runtime_seconds: int = 90,
    deleted: bool = False,
) -> int:
    now = datetime.now(timezone.utc)
    async with Session() as session:
        owner = Client(
            tg_user_id=243000 + seed,
            username=f"repeat-time-pin-lifecycle-{seed}",
            full_name=f"Repeat Time Pin Lifecycle {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100243000 + seed),
            title=f"Repeat Time Pin Lifecycle {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100343000 + seed),
            title=f"Repeat Time Pin Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([channel, target])
        await session.commit()

        runtime_options = (
            {
                "silent": True,
                "pin_on": True,
                "autodelete_seconds": 90,
                "autodelete_report": True,
            }
            if options is None
            else dict(options)
        )
        if runtime_options.get("forward_to") == ["target"]:
            runtime_options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time pin lifecycle"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=runtime_options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        finished_at = now - timedelta(seconds=120)
        publication_meta = dict(publication.meta or {})
        publication_meta[AUTODELETE_RUNTIME_META_KEY] = {
            "deleted": bool(deleted),
            "effective_seconds": int(runtime_seconds),
            "scheduled_at": (finished_at + timedelta(seconds=90)).isoformat(),
        }
        publication.meta = publication_meta
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [9300 + seed]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[9300 + seed],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=finished_at,
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_repeat_time_pin_lifecycle_requires_explicit_composition_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-lifecycle.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_terminal(Session, seed=1)

            async with Session() as session:
                service = CanonicalRepeatTimePinLifecycleAuthorityService(session)
                assert await service.lock_and_prove(publication_id) is None

            async with Session() as session:
                proof = await CanonicalRepeatTimePinLifecycleAuthorityService(
                    session
                ).lock_and_prove(publication_id, allow_time_pin=True)
                assert proof is not None
                assert proof.time_autodelete_seconds == 90
                assert proof.repeat_seconds == 60
                assert proof.pin_on is True
                assert proof.autodelete_report is True
                assert proof.telegram_message_ids == (9301,)
                assert proof.runtime_options == {
                    "silent": True,
                    "pin_on": True,
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                }

            # Plain time proof must not infer the narrower pin composition.
            async with Session() as session:
                assert await CanonicalRepeatTimeLifecycleAuthorityService(
                    session
                ).lock_and_prove(publication_id, allow_time=True) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_lifecycle_rejects_profile_and_generated_drift(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-lifecycle-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                await _seed_terminal(
                    Session,
                    seed=2,
                    options={"autodelete_seconds": 90},
                ),
                await _seed_terminal(
                    Session,
                    seed=3,
                    options={
                        "autodelete_seconds": 90,
                        "pin_on": True,
                        "forward_to": ["target"],
                    },
                ),
                await _seed_terminal(
                    Session,
                    seed=4,
                    options={
                        "autodelete_seconds": 90,
                        "pin_on": True,
                        "autodelete_views": 10,
                    },
                ),
                await _seed_terminal(
                    Session,
                    seed=5,
                    runtime_seconds=91,
                ),
                await _seed_terminal(
                    Session,
                    seed=6,
                    deleted=True,
                ),
            )
            for publication_id in cases:
                async with Session() as session:
                    assert await CanonicalRepeatTimePinLifecycleAuthorityService(
                        session
                    ).lock_and_prove(publication_id, allow_time_pin=True) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
