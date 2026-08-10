from __future__ import annotations

from types import SimpleNamespace

from app.services.content_plan_published_rows import (
    _autodelete_state,
    _repeat_state,
)


def test_malformed_publication_meta_and_repeat_rule_fail_soft() -> None:
    publication = SimpleNamespace(meta=["not", "a", "mapping"])
    schedule = SimpleNamespace(repeat_rule="not-a-mapping")

    assert _autodelete_state(publication) == (False, None, None)  # type: ignore[arg-type]
    assert _repeat_state(schedule) == (False, None)  # type: ignore[arg-type]
