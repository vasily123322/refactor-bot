from __future__ import annotations

from types import SimpleNamespace

from app.core.settings_channel_access import (
    _is_intentional_time_views_fallback,
    _is_post_task_mutation_callback,
)


def test_mutating_content_plan_callbacks_are_classified() -> None:
    assert _is_post_task_mutation_callback("cp_edit_post:44:2026-08-10")
    assert _is_post_task_mutation_callback("cp_delete_post:44:2026-08-10")
    assert _is_post_task_mutation_callback("cp_repeat_off:44")
    assert not _is_post_task_mutation_callback("cp_open_post:44:2026-08-10")


def test_time_views_is_the_only_explicit_legacy_fallback_shape() -> None:
    assert _is_intentional_time_views_fallback(
        SimpleNamespace(payload={"autodelete_seconds": 60, "autodelete_views": 10})
    )
    assert not _is_intentional_time_views_fallback(
        SimpleNamespace(payload={"autodelete_seconds": 60})
    )
    assert not _is_intentional_time_views_fallback(
        SimpleNamespace(payload={"autodelete_views": 10})
    )
    assert not _is_intentional_time_views_fallback(
        SimpleNamespace(payload={"autodelete_seconds": 0, "autodelete_views": 10})
    )
