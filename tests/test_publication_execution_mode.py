from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication
from app.services.posting import PostingService
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
    execution_mode_from_legacy_payload,
    execution_mode_from_runtime_options,
)


async def _session_factory(tmp_path, name: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_channel(session, *, seed: int) -> Channel:
    owner = Client(
        tg_user_id=990000 + seed,
        username=f"execution-mode-{seed}",
        full_name=f"Execution Mode {seed}",
        ui_settings={},
    )
    session.add(owner)
    await session.flush()
    channel = Channel(
        tg_chat_id=-(991000 + seed),
        title=f"Execution Mode {seed}",
        owner_id=int(owner.id),
        is_active=True,
    )
    session.add(channel)
    await session.flush()
    return channel


def test_intrinsic_queue_time_classification_has_no_live_readiness_input() -> None:
    assert execution_mode_from_runtime_options({}) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_runtime_options({"pin_on": True}) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_runtime_options(
        {"autodelete_seconds": 60, "autodelete_views": 10}
    ) == INTENTIONAL_LEGACY_EXECUTION_MODE
    assert execution_mode_from_runtime_options(
        {"autodelete_seconds": 60, "autodelete_report": True}
    ) == INTENTIONAL_LEGACY_EXECUTION_MODE
    assert execution_mode_from_runtime_options(
        {"autodelete_views": 10, "autodelete_report": True, "pin_on": True}
    ) == INTENTIONAL_LEGACY_EXECUTION_MODE
    assert execution_mode_from_runtime_options({"autodelete_report": True}) is None

    # Legacy content/transport fields are irrelevant to the ownership decision.
    assert execution_mode_from_legacy_payload(
        {"type": "text", "text": "hello", "silent": False}
    ) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_legacy_payload(
        {
            "type": "text",
            "text": "hello",
            "repeat_on": True,
            "repeat_seconds": 300,
            "autodelete_views": 50,
        }
    ) == CANONICAL_EXECUTION_MODE
    assert execution_mode_from_legacy_payload(
        {"type": "text", "text": "hello", "repeat_on": True, "repeat_seconds": 0}
    ) is None


class _Bot:
    pass


def test_execution_mode_database_constraint_rejects_unknown_value(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "execution-mode-check.db")
        try:
            async with Session() as session:
                channel = await _seed_channel(session, seed=4)
                await session.commit()
                channel_id = int(channel.id)

            created = await PostingService(_Bot(), Session).schedule(
                channel_id,
                {"type": "text", "text": "constraint"},
                datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
                dedupe_key="execution-mode-constraint",
            )

            async with Session() as session:
                publication = await session.get(Publication, int(created.id))
                assert publication is not None
                publication.execution_mode = "not-an-execution-mode"
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()

                persisted = (
                    await session.execute(
                        select(Publication).where(Publication.id == int(created.id))
                    )
                ).scalar_one()
                assert persisted.execution_mode == CANONICAL_EXECUTION_MODE
        finally:
            await engine.dispose()

    asyncio.run(run())
