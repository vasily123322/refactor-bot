from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_autodelete_runtime_planner import (
    CanonicalPublicationAutodeleteRuntimePlanner,
)
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.canonical_publication_delivery_live_autodelete import (
    CanonicalPublicationDeliveryLiveAutodeleteWriter,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_execution_mode import CANONICAL_EXECUTION_MODE
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


def test_fresh_mixed_claim_materializes_both_runtime_readiness_states() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                owner = Client(
                    tg_user_id=509001,
                    username="mixed-runtime-owner",
                    full_name="Mixed Runtime Owner",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                channel = Channel(
                    tg_chat_id=-100509001,
                    title="Mixed runtime",
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
                                "text": "mixed runtime proof",
                            }
                        ]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=now - timedelta(minutes=1),
                    runtime_options={
                        "autodelete_seconds": 90,
                        "autodelete_views": 250,
                        "autodelete_report": True,
                    },
                )
                publication_id = int(publication.id)
                assert publication.execution_mode == CANONICAL_EXECUTION_MODE
                assert publication.legacy_post_task_id is None
                assert await session.get(PostTask, publication_id) is None

            async with Session() as session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="mixed-runtime-proof",
                    ttl_seconds=180,
                    now=now,
                    allow_time_autodelete=True,
                    allow_views_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options() == {
                    "autodelete_seconds": 90,
                    "autodelete_views": 250,
                    "autodelete_report": True,
                }
                view_state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert view_state is not None
                assert int(view_state.threshold) == 250

            primary_finished_at = now + timedelta(seconds=2)
            materialized_at = now + timedelta(seconds=3)
            context = CanonicalPublicationDeliveryPostSendContext(
                publication_id=publication_id,
                lease=claim.lease,
                plan=claim.plan,
                message_ids=(50901,),
                result_link=None,
                primary_finished_at=primary_finished_at,
            )
            async with Session() as session:
                result = await CanonicalPublicationDeliveryLiveAutodeleteWriter(
                    session
                ).materialize(context, at=materialized_at)
                assert result.outcome == "created"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert dict(publication.meta or {})[AUTODELETE_RUNTIME_META_KEY] == {
                    "deleted": False,
                    "effective_seconds": 90,
                    "scheduled_at": (materialized_at + timedelta(seconds=90)).isoformat(),
                }
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert int(state.threshold) == 250

            async with Session() as session:
                finalized = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[50901],
                    result_link=None,
                    finished_at=primary_finished_at,
                    now=now + timedelta(seconds=4),
                )
                assert finalized.outcome == "published"

            async with Session() as session:
                plan = await CanonicalPublicationAutodeleteRuntimePlanner(session).plan(
                    publication_id
                )
                assert plan is not None
                assert plan.existing is True
                assert plan.effective_seconds == 90
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                assert int(state.threshold) == 250
        finally:
            await engine.dispose()

    asyncio.run(run())
