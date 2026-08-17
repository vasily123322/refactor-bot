from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.models import Client
from app.services.studio_channel_onboarding_requests import (
    StudioChannelOnboardingRequestService,
)


class FakeProvider:
    async def prepare(self, *, tg_user_id: int, request_id: int) -> str:
        return f"prepared-{request_id}"


def test_concurrent_shared_chat_claim_has_exactly_one_winner() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "onboarding.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session:
                    client = Client(
                        tg_user_id=7001,
                        username="owner",
                        full_name="Owner",
                    )
                    session.add(client)
                    await session.commit()
                    await session.refresh(client)
                    client_id = int(client.id)

                service = StudioChannelOnboardingRequestService(
                    session_factory=factory,
                    provider=FakeProvider(),
                )
                prepared = await service.prepare(
                    client_id=client_id,
                    tg_user_id=7001,
                )

                left, right = await asyncio.gather(
                    service.claim_shared(
                        request_id=prepared.request_id,
                        sender_tg_user_id=7001,
                        selected_chat_id=-10001,
                    ),
                    service.claim_shared(
                        request_id=prepared.request_id,
                        sender_tg_user_id=7001,
                        selected_chat_id=-10002,
                    ),
                )

                winners = [
                    chat_id
                    for result, chat_id in ((left, -10001), (right, -10002))
                    if result.ok
                ]
                assert len(winners) == 1

                view = await service.get_for_client(
                    client_id=client_id,
                    request_id=prepared.request_id,
                )
                assert view is not None
                assert view.status == "processing"
                assert view.selected_chat_id == winners[0]
            finally:
                await engine.dispose()

    asyncio.run(run())
