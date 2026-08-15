from __future__ import annotations

from dataclasses import fields

from app.api.studio.schemas import PlannerEntryResponse
from app.services.planner import PlannerEntry


def test_planner_read_models_expose_only_canonical_delivery_identity():
    entry_fields = {field.name for field in fields(PlannerEntry)}
    response_fields = set(PlannerEntryResponse.model_fields)

    assert "schedule_id" in entry_fields
    assert "publication_id" in entry_fields
    assert "legacy_post_task_id" not in entry_fields
    assert "legacy_post_task_id" not in response_fields
