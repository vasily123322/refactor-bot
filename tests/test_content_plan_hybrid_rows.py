from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton

from app.bot.routers.utils.content_plan_hybrid import (
    TimedContentPlanButtonRow,
    canonical_only_published_button_rows,
    canonical_published_button_row,
    merge_timed_content_plan_rows,
)
from app.services.content_plan_published_rows import PublishedContentPlanRow


def _row(**updates) -> PublishedContentPlanRow:
    values = {
        "publication_id": 77,
        "legacy_post_task_id": None,
        "scheduled_at": datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc),
        "title": "Canonical published row",
        "autodeleted": False,
        "autodelete_seconds": 7200,
        "autodelete_views": None,
        "repeat_enabled": False,
        "repeat_seconds": None,
    }
    values.update(updates)
    return PublishedContentPlanRow(**values)


def test_canonical_only_non_repeat_row_uses_publication_identity() -> None:
    rendered = canonical_published_button_row(
        _row(),
        date_iso="2026-08-10",
        tz_code="UTC",
    )

    assert rendered is not None
    assert rendered.scheduled_at == datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc)
    assert len(rendered.buttons) == 1
    button = rendered.buttons[0]
    assert button.callback_data == "cp_open_pub:77:2026-08-10"
    assert button.text.startswith("12:30 ✅ Canonical published row")
    assert "🗑️ 2ч" in button.text


def test_linked_or_repeat_canonical_rows_are_not_injected() -> None:
    assert (
        canonical_published_button_row(
            _row(legacy_post_task_id=44),
            date_iso="2026-08-10",
            tz_code="UTC",
        )
        is None
    )
    assert (
        canonical_published_button_row(
            _row(repeat_enabled=True, repeat_seconds=3600),
            date_iso="2026-08-10",
            tz_code="UTC",
        )
        is None
    )


def test_hybrid_rows_keep_chronological_order() -> None:
    legacy = TimedContentPlanButtonRow(
        scheduled_at=datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc),
        buttons=[InlineKeyboardButton(text="legacy", callback_data="legacy")],
    )
    canonical = TimedContentPlanButtonRow(
        scheduled_at=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
        buttons=[InlineKeyboardButton(text="canonical", callback_data="canonical")],
    )

    merged = merge_timed_content_plan_rows([legacy], [canonical])

    assert [row[0].callback_data for row in merged] == ["canonical", "legacy"]


def test_canonical_listing_failure_preserves_legacy_enhancement_boundary(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot.routers.utils import content_plan_hybrid as hybrid_module

        async def fail(*args, **kwargs):
            raise RuntimeError("canonical db failure with secret-like detail")

        monkeypatch.setattr(hybrid_module, "list_published_content_plan_rows", fail)
        rows = await canonical_only_published_button_rows(
            object(),  # type: ignore[arg-type]
            channel_id=12,
            start_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            date_iso="2026-08-10",
            tz_code="UTC",
        )
        assert rows == []

    asyncio.run(run())
