from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import AISource, GrabSource
from app.domain.sources.models import SourceConnector
from app.repositories.sources_v2 import SourcesRepo
from app.services.legacy_source_mirror import LegacySourceMirror
from app.services.source_doctor import SourceDoctor


def test_legacy_sources_are_mirrored_idempotently_without_assuming_reuse_rights() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                session.add_all(
                    [
                        AISource(
                            channel_id=7,
                            source_type="rss",
                            source_value="https://example.com/rss.xml",
                            mode="summary",
                            enabled=True,
                            citation_enabled=True,
                        ),
                        GrabSource(
                            source_chat_id=-100123,
                            target_channel_id=7,
                            filter_flags={"text": 1, "photo": 1},
                        ),
                    ]
                )
                await session.commit()

                mirror = LegacySourceMirror(session)
                assert await mirror.sync_channel(7) >= 2
                first = await SourcesRepo(session).list_connectors(7)
                assert len(first) == 2
                assert {row.kind for row in first} == {"rss", "telegram"}
                assert all(row.reuse_policy == "reference_only" for row in first)
                grab = next(row for row in first if row.legacy_grab_source_id is not None)
                ai = next(row for row in first if row.legacy_ai_source_id is not None)
                assert grab.mode == "mirror"
                assert grab.config["filter_flags"]["photo"] == 1
                assert grab.config["lifecycle_editable"] is False

                # reuse_policy has no field in legacy AISource. Once an operator
                # chooses it in Sources v2, compatibility sync must preserve it.
                ai.reuse_policy = "rewrite_with_attribution"
                await session.commit()
                await mirror.sync_channel(7)
                second = await SourcesRepo(session).list_connectors(7)
                assert len(second) == 2
                ai_again = next(row for row in second if row.legacy_ai_source_id is not None)
                grab_again = next(
                    row for row in second if row.legacy_grab_source_id is not None
                )
                assert ai_again.reuse_policy == "rewrite_with_attribution"
                assert grab_again.reuse_policy == "reference_only"
                assert grab_again.enabled is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_source_doctor_reports_web_health_without_leaking_response_content() -> None:
    async def run() -> None:
        async def web_probe(url: str) -> str:
            assert url == "https://example.com/feed"
            return "private page body"

        connector = SourceConnector(
            channel_id=1,
            kind="rss",
            value="https://example.com/feed",
            enabled=True,
        )
        result = await SourceDoctor(web_probe=web_probe).check(connector)
        assert result.status == "healthy"
        assert result.success is True
        assert result.auth_state == "not_required"
        assert result.health["sample_size"] == len("private page body")
        assert "private page body" not in str(result.health)
        assert result.capabilities["history"] is True

    asyncio.run(run())


def test_source_doctor_fails_closed_and_marks_telegram_probe_state() -> None:
    async def run() -> None:
        broken = SourceConnector(
            channel_id=1,
            kind="url",
            value="file:///etc/passwd",
            enabled=True,
        )
        result = await SourceDoctor().check(broken)
        assert result.status == "broken"
        assert "HTTP" in str(result.reason)

        telegram = SourceConnector(
            channel_id=1,
            kind="telegram",
            value="@example",
            enabled=True,
        )
        tg_result = await SourceDoctor().check(telegram)
        assert tg_result.status == "unknown"
        assert tg_result.auth_state == "session_required"
        assert tg_result.health["checked"] is False

    asyncio.run(run())
