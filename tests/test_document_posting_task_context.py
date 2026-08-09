from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.services.document_posting import DocumentPostingService
from app.services.telegram_renderer import TelegramRenderError


def _rich_payload(**extra) -> dict:
    document = PostDocument(
        mode="rich",
        blocks=[{"id": "p1", "type": "paragraph", "content": "Task context"}],
    )
    return {"type": "rich_document", "post_document": document.to_dict(), **extra}


def test_rich_dispatch_prefers_scheduler_task_channel_over_payload_marker(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'task-context.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                task = PostTask(channel_id=1201, status="processing", payload={})
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = int(task.id)

            service = DocumentPostingService(SimpleNamespace(), Session)
            captured: list[int | None] = []

            async def fake_send_document(chat_id, document, *, asset_channel_id=None):
                captured.append(asset_channel_id)
                return [501]

            service.send_document = fake_send_document  # type: ignore[method-assign]
            result = await service._dispatch(
                -1001201,
                _rich_payload(
                    _post_task_id=task_id,
                    _content_channel_id=999999,
                ),
            )
            assert result == [501]
            assert captured == [1201]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rich_dispatch_does_not_fall_back_to_marker_when_task_context_is_missing(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'missing-task-context.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            service = DocumentPostingService(SimpleNamespace(), Session)

            with pytest.raises(TelegramRenderError, match="task context not found"):
                await service._dispatch(
                    -1001301,
                    _rich_payload(
                        _post_task_id=999999,
                        _content_channel_id=1301,
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rich_dispatch_rejects_invalid_task_context_even_with_legacy_marker() -> None:
    async def run() -> None:
        service = DocumentPostingService(SimpleNamespace(), lambda: None)
        with pytest.raises(TelegramRenderError, match="task context is invalid"):
            await service._dispatch(
                -1001302,
                _rich_payload(
                    _post_task_id="not-an-id",
                    _content_channel_id=1302,
                ),
            )

    asyncio.run(run())


def test_rich_dispatch_keeps_historical_marker_fallback_without_task_context() -> None:
    async def run() -> None:
        service = DocumentPostingService(SimpleNamespace(), lambda: None)
        captured: list[int | None] = []

        async def fake_send_document(chat_id, document, *, asset_channel_id=None):
            captured.append(asset_channel_id)
            return [502]

        service.send_document = fake_send_document  # type: ignore[method-assign]
        result = await service._dispatch(
            -1001303,
            _rich_payload(_content_channel_id="1303"),
        )
        assert result == [502]
        assert captured == [1303]

    asyncio.run(run())
