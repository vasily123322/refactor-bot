from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge


class _State:
    def __init__(self) -> None:
        self.data = {"cp_show_repeats": False, "cp_page": 0}

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


class _Message:
    def __init__(self) -> None:
        self.rendered = None

    async def edit_text(self, text: str, **kwargs) -> None:
        self.rendered = (text, kwargs.get("reply_markup"))


class _Callback:
    def __init__(self, *, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _Message()
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", *, show_alert: bool = False) -> None:
        self.answers.append((str(text), bool(show_alert)))


class _Bot:
    async def get_chat(self, chat_id: int):
        return SimpleNamespace(username="canonical_listing_test")


def test_render_keeps_published_row_after_post_task_delete(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        from app.bot.routers import content_plan

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'hybrid-render.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled = datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc)

            async with Session() as session:
                owner = Client(
                    tg_user_id=76001,
                    username="owner",
                    full_name="Owner",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-10076001,
                    title="Hybrid canonical channel",
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
                                "text": "Visible after PostTask retirement",
                            }
                        ]
                    ),
                    created_by_tg_user_id=76001,
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=scheduled,
                )
                publication_id = int(publication.id)
                task_id = int(publication.legacy_post_task_id or 0)
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                task = await session.get(PostTask, task_id)
                assert schedule is not None and task is not None
                publication.status = "published"
                schedule.status = "completed"
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()
                channel_id = int(channel.id)

            monkeypatch.setattr(content_plan, "AsyncSessionLocal", Session)
            monkeypatch.setattr(content_plan, "tg_bot", _Bot())
            callback = _Callback(user_id=76001)
            await content_plan._render_content_plan(  # noqa: SLF001 - integration boundary
                callback,  # type: ignore[arg-type]
                _State(),  # type: ignore[arg-type]
                channel_id,
                datetime(2026, 8, 10, tzinfo=timezone.utc),
            )

            assert callback.answers == []
            assert callback.message.rendered is not None
            _, keyboard = callback.message.rendered
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data
            ]
            assert f"cp_open_pub:{publication_id}:2026-08-10" in callbacks
            assert not any(value.startswith("cp_open_post:") for value in callbacks)
        finally:
            await engine.dispose()

    asyncio.run(run())
