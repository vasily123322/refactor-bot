from __future__ import annotations

import inspect
from datetime import datetime, timezone

from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.api.studio.schemas import PlannerEntryResponse, PublicationResponse


def _config() -> StudioConfig:
    return StudioConfig(
        enabled=True,
        host="127.0.0.1",
        port=8080,
        public_url="https://studio.example.test",
        init_data_max_age_seconds=86400,
        cors_origins=(),
    )


def test_studio_public_contract_hides_legacy_post_task_id() -> None:
    publication = PublicationResponse(
        id=1,
        content_item_id=2,
        content_revision=3,
        channel_id=4,
        status="queued",
        schedule_entry_id=5,
        legacy_post_task_id=999,
    )
    planner = PlannerEntryResponse(
        schedule_id=5,
        channel_id=4,
        content_item_id=2,
        content_revision=3,
        content_title="Post",
        content_kind="post",
        scheduled_at=datetime.now(timezone.utc),
        timezone="UTC",
        schedule_status="pending",
        repeat_rule={},
        publication_id=1,
        publication_status="queued",
        telegram_message_ids=None,
        result_link=None,
        last_error=None,
        attempt_number=None,
        attempt_status=None,
        attempt_started_at=None,
        attempt_finished_at=None,
        legacy_post_task_id=999,
    )

    assert "legacy_post_task_id" not in publication.model_dump()
    assert "legacy_post_task_id" not in planner.model_dump()

    schemas = create_studio_app(_config()).openapi()["components"]["schemas"]
    assert "legacy_post_task_id" not in schemas["PublicationResponse"]["properties"]
    assert "legacy_post_task_id" not in schemas["PlannerEntryResponse"]["properties"]


def test_studio_schedule_response_does_not_read_compatibility_task_identity() -> None:
    assert "legacy_post_task_id" not in inspect.getsource(create_studio_app)
