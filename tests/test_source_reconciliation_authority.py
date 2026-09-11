from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_reconciliation import (
    SourceIngestionReconciliationService,
    SourceProjection,
    SourceProjectionUpdateMode,
    SourceReconciliationError,
)


def test_reconciliation_fails_closed_if_document_routing_drifts_from_connector() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=81,
                    kind="rss",
                    value="https://example.com/feed.xml",
                )
                service = SourceIngestionReconciliationService(session)
                first = await service.reconcile(
                    connector,
                    SourceProjection(external_id="authority-1", content="Body"),
                )

                # Simulate persisted routing drift without changing the trusted
                # connector authority. Lifecycle reconciliation must not create a
                # second candidate for another channel or silently reroute the row.
                first.document.channel_id = 82
                await session.commit()

                with pytest.raises(SourceReconciliationError, match="routing disagrees"):
                    await service.reconcile(
                        connector,
                        SourceProjection(
                            external_id="authority-1",
                            metadata={"lifecycle": "approved"},
                            update_mode=SourceProjectionUpdateMode.LIFECYCLE,
                        ),
                    )
                candidate = (
                    await session.execute(select(ContentCandidate))
                ).scalar_one()
                assert candidate.channel_id == 81
        finally:
            await engine.dispose()

    asyncio.run(run())
