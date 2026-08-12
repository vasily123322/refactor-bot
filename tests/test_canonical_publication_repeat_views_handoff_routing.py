from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.repositories.content import ContentRepo
from app.services import canonical_publication_repeat_handoff_executor as module
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimResult,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=218990,
            username="repeat-views-route-facts",
            full_name="Repeat Views Route Facts",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100218990,
            title="Repeat Views Route Facts",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "route facts"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"autodelete_views": 11},
        )
        return int(publication.id)


class _Executor:
    holder = "repeat-views-route-facts"
    lease_seconds = 180
    allow_repeat = True
    allow_time_autodelete = False
    allow_views_autodelete = True
    allow_repeat_views = True
    allow_repeat_views_pin = True

    async def execute(self, publication_id: int):
        raise AssertionError("linked repeat must not route to direct execute")

    async def execute_claim(self, claim):
        raise AssertionError("fake transfer intentionally never returns a claim")


def test_linked_repeat_router_forwards_exact_repeat_views_facts(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-route-facts.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(Session)
            seen: dict[str, object] = {}

            class _FakeAtomicService:
                def __init__(self, session) -> None:
                    self.session = session

                async def claim_linked_repeat(self, publication_id, **kwargs):
                    seen.update(kwargs)
                    return CanonicalPublicationAtomicHandoffClaimResult(
                        publication_id=int(publication_id),
                        outcome="ineligible",
                    )

            monkeypatch.setattr(
                module,
                "CanonicalPublicationLinkedRepeatAtomicHandoffService",
                _FakeAtomicService,
            )
            result = await CanonicalPublicationRepeatHandoffExecutor(
                executor=_Executor(),
                session_factory=Session,
            ).execute(publication_id)
            assert result.outcome == "ineligible"
            assert seen == {
                "holder": "repeat-views-route-facts",
                "ttl_seconds": 180,
                "allow_repeat": True,
                "allow_views_autodelete": True,
                "allow_repeat_views": True,
                "allow_repeat_views_pin": True,
            }
        finally:
            await engine.dispose()

    asyncio.run(run())