from __future__ import annotations

import asyncio

from app.services.content_plan_publication_links import (
    content_plan_open_callback,
    published_publication_ids_for_legacy_tasks,
)


class _FailingSession:
    async def execute(self, statement):
        raise RuntimeError("temporary canonical read failure with secret-like detail")


def test_canonical_lookup_failure_falls_back_to_legacy_callback() -> None:
    async def run() -> None:
        links = await published_publication_ids_for_legacy_tasks(
            _FailingSession(),  # type: ignore[arg-type]
            channel_id=12,
            post_task_ids=[44],
        )
        assert links == {}
        assert content_plan_open_callback(
            post_task_id=44,
            date_iso="2026-08-10",
            published_publication_ids=links,
        ) == "cp_open_post:44:2026-08-10"

    asyncio.run(run())
