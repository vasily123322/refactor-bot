from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.services.content_plan_publication_links import (
    legacy_content_plan_open_callback,
    list_linked_content_plan_publications,
)


class _FailingSession:
    async def execute(self, statement):
        raise RuntimeError("temporary canonical read failure with secret-like detail")


def test_canonical_lookup_failure_preserves_explicit_legacy_fallback() -> None:
    async def run() -> None:
        links = await list_linked_content_plan_publications(
            _FailingSession(),  # type: ignore[arg-type]
            channel_id=12,
            start_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
        assert links == []
        assert legacy_content_plan_open_callback(
            post_task_id=44,
            date_iso="2026-08-10",
        ) == "cp_open_post:44:2026-08-10"

    asyncio.run(run())
