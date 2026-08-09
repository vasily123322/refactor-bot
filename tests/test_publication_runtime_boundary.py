from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.repositories.content import ContentRepo
from app.services.planner import PlannerService
from app.services.publication_bridge import LegacyPublicationBridge, PublicationBridgeError
from app.services.telegram_results import normalize_telegram_result_link


def test_telegram_result_link_allows_only_canonical_tme_https() -> None:
    assert (
        normalize_telegram_result_link(" https://t.me/example/101 ")
        == "https://t.me/example/101"
    )
    assert (
        normalize_telegram_result_link("https://T.ME/c/12345/678")
        == "https://t.me/c/12345/678"
    )
    assert normalize_telegram_result_link("javascript:alert(1)") is None
    assert normalize_telegram_result_link("http://t.me/example/101") is None
    assert normalize_telegram_result_link("https://evil.example/101") is None
    assert normalize_telegram_result_link("https://t.me.evil.example/101") is None
    assert normalize_telegram_result_link("https://t.me@evil.example/101") is None
    assert normalize_telegram_result_link("https://user@t.me/example/101") is None
    assert normalize_telegram_result_link("https://t.me/example/101?token=secret") is None
    assert normalize_telegram_result_link("https://t.me/example/101#fragment") is None
    assert normalize_telegram_result_link("https://t.me/") is None


def test_queue_rejects_runtime_options_that_override_transport_owned_fields() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=601,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Canonical"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                reserved_options = [
                    {"text": "spoofed content"},
                    {"type": "photo"},
                    {"repeat_on": True},
                    {"repeat_seconds": 1},
                    {"result_ids": [999]},
                    {"result_link": "javascript:alert(1)"},
                    {"autodelete_at": "2030-01-01T00:00:00Z"},
                    {"autodelete_effective_seconds": 1},
                    {"autodeleted": True},
                    {"autosign_applied": True},
                    {"repeat_group_id": 777},
                    {"_publication_id": 777},
                    {"": "empty"},
                ]
                for options in reserved_options:
                    with pytest.raises(PublicationBridgeError, match="runtime option is reserved"):
                        await bridge.queue(
                            content_item_id=int(item.id),
                            runtime_options=options,
                        )
                    await session.rollback()

                publication = await bridge.queue(
                    content_item_id=int(item.id),
                    runtime_options={
                        "pin_on": True,
                        "silent": True,
                        "forward_to": [11, 12],
                        "autodelete_seconds": 900,
                        "nested": {"mode": "extension"},
                    },
                )
                task = await session.get(PostTask, int(publication.legacy_post_task_id))
                assert task is not None
                assert task.payload["text"] == "Canonical"
                assert task.payload["pin_on"] is True
                assert task.payload["silent"] is True
                assert task.payload["forward_to"] == [11, 12]
                assert task.payload["autodelete_seconds"] == 900
                assert task.payload["nested"] == {"mode": "extension"}
                assert publication.meta["runtime_options"]["nested"] == {
                    "mode": "extension"
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reconcile_drops_unsafe_legacy_result_link_before_planner() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=602,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Link"}]
                    ),
                )
                bridge = LegacyPublicationBridge(session)
                publication = await bridge.queue(content_item_id=int(item.id))
                schedule_id = int(publication.schedule_entry_id)
                task = await session.get(PostTask, int(publication.legacy_post_task_id))
                assert task is not None
                task.status = "done"
                task.payload = {
                    **dict(task.payload or {}),
                    "result_ids": [60201],
                    "result_link": "javascript:alert(document.domain)",
                }
                await session.commit()

                publication = await bridge.reconcile(int(publication.id))
                assert publication.status == "published"
                assert publication.telegram_message_ids == [60201]
                assert publication.result_link is None

                planner = await PlannerService(session).get_entry(
                    channel_id=602,
                    schedule_id=schedule_id,
                )
                assert planner.publication_status == "published"
                assert planner.result_link is None
        finally:
            await engine.dispose()

    asyncio.run(run())
