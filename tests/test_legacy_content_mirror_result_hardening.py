from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, PublicationAttempt
from app.services.legacy_terminal_content_mirror import (
    mirror_unlinked_terminal_legacy_tasks,
)
from app.services.scheduler_errors import GENERIC_SCHEDULER_ERROR


def test_legacy_mirror_redacts_untrusted_terminal_transport_fields() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            # Build credential-like input at runtime so tracked-secret scanners never
            # see a token-shaped literal in the repository itself.
            secret = ":".join(("123456", "FAKE_BOT_TOKEN"))
            raw_error = (
                "telegram request failed "
                f"https://api.telegram.org/bot{secret}/sendMessage?access_token=private"
            )
            async with Session() as session:
                task = PostTask(
                    channel_id=901,
                    status="failed",
                    error=raw_error,
                    payload={
                        "type": "text",
                        "text": "Historical failed post",
                        "result_ids": [1001, "not-an-id", 1002],
                        "result_link": "https://evil.example/post/1002?token=private",
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)

                mirrored, skipped = await mirror_unlinked_terminal_legacy_tasks(session)
                assert mirrored == 1
                assert skipped == 0

                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task_id
                        )
                    )
                ).scalar_one()
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication.id
                        )
                    )
                ).scalar_one()

                assert publication.status == "failed"
                assert publication.telegram_message_ids is None
                assert publication.result_link is None
                assert publication.last_error == GENERIC_SCHEDULER_ERROR
                assert attempt.status == "failed"
                assert attempt.telegram_message_ids is None
                assert attempt.error == GENERIC_SCHEDULER_ERROR

                # The migration read model is sanitized without pretending the
                # historical compatibility row itself was rewritten or repaired.
                await session.refresh(task)
                assert task.error == raw_error
                assert secret in task.error
                assert task.payload["result_ids"] == [1001, "not-an-id", 1002]
                assert task.payload["result_link"].startswith("https://evil.example/")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_mirror_preserves_valid_scheduler_results() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                task = PostTask(
                    channel_id=902,
                    status="done",
                    payload={
                        "type": "text",
                        "text": "Historical published post",
                        "result_ids": ["2001", 2002],
                        "result_link": "https://t.me/example_channel/2002",
                    },
                )
                session.add(task)
                await session.commit()

                mirrored, skipped = await mirror_unlinked_terminal_legacy_tasks(session)
                assert mirrored == 1
                assert skipped == 0

                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task.id
                        )
                    )
                ).scalar_one()
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication.id
                        )
                    )
                ).scalar_one()

                assert publication.status == "published"
                assert publication.telegram_message_ids == [2001, 2002]
                assert publication.result_link == "https://t.me/example_channel/2002"
                assert publication.last_error is None
                assert attempt.telegram_message_ids == [2001, 2002]
                assert attempt.error is None
        finally:
            await engine.dispose()

    asyncio.run(run())
