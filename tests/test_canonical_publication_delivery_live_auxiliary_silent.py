from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, ChannelSettings, Client, PostTask
from app.repositories.admin import AdminConfigRepo
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlanner,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.publication_bridge import LegacyPublicationBridge


def test_live_auxiliary_planner_accepts_exact_silent_runtime_snapshot(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-aux-silent.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)

            async with Session() as session:
                owner = Client(
                    tg_user_id=185001,
                    username="silent-live-owner",
                    full_name="Silent Live Owner",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-100185001,
                    title="Silent Live Channel",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add(channel)
                await session.flush()
                session.add(
                    ChannelSettings(
                        channel_id=int(channel.id),
                        autosign=None,
                        split_rules=None,
                        filters={"tz": "UTC"},
                    )
                )
                await session.commit()

                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[
                            {
                                "id": "b1",
                                "type": "text",
                                "text": "Silent live auxiliary proof",
                            }
                        ]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=scheduled_at,
                    runtime_options={"silent": True},
                )
                task = await session.get(
                    PostTask,
                    int(publication.legacy_post_task_id or 0),
                )
                assert task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                await AdminConfigRepo(session).set_log_chat(-999185001)
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=int(publication.id),
                    holder="silent-live-aux-test",
                    ttl_seconds=180,
                    now=scheduled_at + timedelta(seconds=1),
                )
                assert claim is not None
                assert claim.plan.runtime_options() == {"silent": True}

            finished_at = scheduled_at + timedelta(seconds=2)
            context = CanonicalPublicationDeliveryPostSendContext(
                publication_id=int(claim.plan.publication_id),
                lease=claim.lease,
                plan=claim.plan,
                message_ids=(2201,),
                result_link="https://t.me/c/185001/2201",
                primary_finished_at=finished_at,
            )
            async with Session() as session:
                planned = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(context, at=finished_at)
                assert planned is not None
                assert planned.owner_notice is not None
                assert planned.owner_notice.owner_tg_user_id == 185001
                assert planned.admin_log is not None
                assert planned.admin_log.log_chat_id == -999185001
        finally:
            await engine.dispose()

    asyncio.run(run())
