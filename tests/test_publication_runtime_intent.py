from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge


def test_queue_persists_runtime_intent_outside_legacy_task_payload() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            runtime_options = {
                "pin_on": True,
                "silent": True,
                "forward_to": [11, 12],
                "autodelete_seconds": 900,
                "nested": {"mode": "original"},
            }
            metadata = {
                "scheduled_from": "studio",
                "runtime_options": {"spoofed": True},
            }

            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=95,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Runtime intent"}]
                    ),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id,
                    runtime_options=runtime_options,
                    metadata=metadata,
                )
                publication_id = int(publication.id)
                schedule_id = int(publication.schedule_entry_id or 0)
                task_id = int(publication.legacy_post_task_id or 0)
                assert schedule_id > 0 and task_id > 0

                task = await session.get(PostTask, task_id)
                assert task is not None
                for key, value in runtime_options.items():
                    assert task.payload[key] == value

                # Scheduler-generated fields are not part of canonical queue intent.
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [501],
                    "result_link": "https://t.me/example/501",
                    "autodelete_at": "2030-01-01T00:00:00+00:00",
                }
                await session.commit()

            # Mutating caller-owned nested structures after queue must not mutate
            # persisted Schedule/Publication metadata or the committed task payload.
            runtime_options["forward_to"].append(99)
            runtime_options["nested"]["mode"] = "mutated"
            metadata["scheduled_from"] = "mutated"

            async with Session() as verify_session:
                schedule = await verify_session.get(ScheduleEntry, schedule_id)
                publication = await verify_session.get(Publication, publication_id)
                task = await verify_session.get(PostTask, task_id)
                assert schedule is not None and publication is not None and task is not None

                expected = {
                    "pin_on": True,
                    "silent": True,
                    "forward_to": [11, 12],
                    "autodelete_seconds": 900,
                    "nested": {"mode": "original"},
                }
                assert schedule.meta["scheduled_from"] == "studio"
                assert publication.meta["scheduled_from"] == "studio"
                assert schedule.meta["runtime_options"] == expected
                assert publication.meta["runtime_options"] == expected
                assert schedule.meta["runtime_options"] is not publication.meta["runtime_options"]
                assert schedule.meta["legacy_post_task_id"] == task_id

                assert "result_ids" not in schedule.meta["runtime_options"]
                assert "result_link" not in schedule.meta["runtime_options"]
                assert "autodelete_at" not in schedule.meta["runtime_options"]
                assert task.payload["result_ids"] == [501]
        finally:
            await engine.dispose()

    asyncio.run(run())
