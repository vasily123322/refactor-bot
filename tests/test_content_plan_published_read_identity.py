from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone

from app.bot.routers.utils.content_plan_hybrid import canonical_published_button_row
from app.services.content_plan_published_rows import PublishedContentPlanRow


def _published_row(*, has_link: bool) -> PublishedContentPlanRow:
    return PublishedContentPlanRow(
        publication_id=91,
        has_legacy_post_task_link=has_link,
        scheduled_at=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
        title="Canonical publication",
        autodeleted=False,
        autodelete_seconds=None,
        autodelete_views=None,
        repeat_enabled=False,
        repeat_seconds=None,
    )


def test_published_read_dto_does_not_expose_post_task_identity():
    field_names = {field.name for field in fields(PublishedContentPlanRow)}
    assert "legacy_post_task_id" not in field_names
    assert "has_legacy_post_task_link" in field_names


def test_linked_published_row_remains_deduplicated_without_exposing_post_task_id():
    assert (
        canonical_published_button_row(
            _published_row(has_link=True),
            date_iso="2026-08-15",
            tz_code="UTC",
        )
        is None
    )


def test_unlinked_published_row_uses_publication_identity_callback():
    rendered = canonical_published_button_row(
        _published_row(has_link=False),
        date_iso="2026-08-15",
        tz_code="UTC",
    )
    assert rendered is not None
    assert len(rendered.buttons) == 1
    assert rendered.buttons[0].callback_data == "cp_open_pub:91:2026-08-15"
