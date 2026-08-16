from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton

from app.bot.routers.utils.content_plan_hybrid import (
    TimedContentPlanButtonRow,
    canonical_published_button_row,
    merge_timed_content_plan_rows,
)
from app.services.content_plan_published_rows import PublishedContentPlanRow
from app.services.publication_editor import publication_open_callback


DATE_ISO = "2026-08-16"
BASE_TIME = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)


def _published_row(
    *,
    publication_id: int = 42,
    scheduled_at: datetime = BASE_TIME,
    linked: bool = True,
    repeat_enabled: bool = False,
) -> PublishedContentPlanRow:
    return PublishedContentPlanRow(
        publication_id=publication_id,
        has_legacy_post_task_link=linked,
        scheduled_at=scheduled_at,
        title="Canonical title",
        autodeleted=False,
        autodelete_seconds=None,
        autodelete_views=None,
        repeat_enabled=repeat_enabled,
        repeat_seconds=3600 if repeat_enabled else None,
    )


def _timed_row(
    callback_data: str,
    *,
    scheduled_at: datetime = BASE_TIME,
    text: str = "legacy",
    extra_callbacks: tuple[str, ...] = (),
    canonical_identity: str | None = None,
) -> TimedContentPlanButtonRow:
    return TimedContentPlanButtonRow(
        scheduled_at=scheduled_at,
        buttons=[
            InlineKeyboardButton(text=text, callback_data=callback_data),
            *[
                InlineKeyboardButton(text="extra", callback_data=extra_callback)
                for extra_callback in extra_callbacks
            ],
        ],
        canonical_presentation_identity=canonical_identity,
    )


def _canonical_row(
    *,
    publication_id: int = 42,
    scheduled_at: datetime = BASE_TIME,
    repeat_enabled: bool = False,
) -> TimedContentPlanButtonRow:
    rendered = canonical_published_button_row(
        _published_row(
            publication_id=publication_id,
            scheduled_at=scheduled_at,
            repeat_enabled=repeat_enabled,
        ),
        date_iso=DATE_ISO,
        tz_code="UTC",
    )
    assert rendered is not None
    return rendered


def test_linked_non_repeat_prefers_single_canonical_button() -> None:
    canonical = _canonical_row(publication_id=42)
    identity = publication_open_callback(42, DATE_ISO)
    legacy = _timed_row(identity, text="compatibility")

    merged = merge_timed_content_plan_rows([legacy], [canonical])

    assert len(merged) == 1
    assert merged[0][0].callback_data == identity
    assert merged[0][0].text == canonical.buttons[0].text


def test_canonical_button_uses_exact_publication_identity() -> None:
    canonical = _canonical_row(publication_id=731)

    assert canonical.canonical_presentation_identity == publication_open_callback(731, DATE_ISO)
    assert canonical.buttons[0].callback_data == publication_open_callback(731, DATE_ISO)


def test_exact_one_to_one_identity_suppresses_only_compatibility_duplicate() -> None:
    canonical = _canonical_row(publication_id=42)
    identity = publication_open_callback(42, DATE_ISO)
    duplicate = _timed_row(identity, text="compatibility")
    unrelated = _timed_row("cp_open_post:999", text="historical")

    merged = merge_timed_content_plan_rows([duplicate, unrelated], [canonical])

    callbacks = [button.callback_data for row in merged for button in row]
    assert callbacks.count(identity) == 1
    assert "cp_open_post:999" in callbacks


def test_unmatched_legacy_row_is_preserved() -> None:
    canonical = _canonical_row(publication_id=42)
    legacy = _timed_row("cp_open_post:501", text="historical")

    merged = merge_timed_content_plan_rows([legacy], [canonical])

    callbacks = [button.callback_data for row in merged for button in row]
    assert "cp_open_post:501" in callbacks
    assert publication_open_callback(42, DATE_ISO) in callbacks


def test_multiple_canonical_matches_fail_closed_and_keep_legacy() -> None:
    identity = publication_open_callback(42, DATE_ISO)
    canonical_a = _timed_row(
        identity,
        text="canonical-a",
        canonical_identity=identity,
    )
    canonical_b = _timed_row(
        identity,
        text="canonical-b",
        canonical_identity=identity,
    )
    legacy = _timed_row(identity, text="compatibility")

    merged = merge_timed_content_plan_rows([legacy], [canonical_a, canonical_b])

    texts = [button.text for row in merged for button in row]
    assert "compatibility" in texts
    assert texts.count("canonical-a") == 1
    assert texts.count("canonical-b") == 1


def test_ambiguous_identity_does_not_fall_back_to_text_or_time_heuristics() -> None:
    canonical = _canonical_row(publication_id=42)
    same_time_and_text_but_other_identity = _timed_row(
        "cp_open_post:42",
        scheduled_at=canonical.scheduled_at,
        text=canonical.buttons[0].text,
    )

    merged = merge_timed_content_plan_rows(
        [same_time_and_text_but_other_identity],
        [canonical],
    )

    callbacks = [button.callback_data for row in merged for button in row]
    assert callbacks == [
        "cp_open_post:42",
        publication_open_callback(42, DATE_ISO),
    ]


def test_repeat_enabled_row_has_posttask_free_canonical_presentation() -> None:
    canonical = _canonical_row(publication_id=42, repeat_enabled=True)

    assert canonical.buttons[0].callback_data == publication_open_callback(42, DATE_ISO)
    assert "🔁" in canonical.buttons[0].text
    assert canonical.canonical_presentation_identity == publication_open_callback(42, DATE_ISO)


def test_repeat_compatibility_row_is_suppressed_by_exact_canonical_identity() -> None:
    identity = publication_open_callback(42, DATE_ISO)
    canonical = _canonical_row(publication_id=42, repeat_enabled=True)
    compatibility = _timed_row(
        identity,
        text="repeat compatibility",
        extra_callbacks=("cp_repeat_off:42",),
    )

    merged = merge_timed_content_plan_rows([compatibility], [canonical])

    assert len(merged) == 1
    assert [button.callback_data for button in merged[0]] == [identity]
    assert "🔁" in merged[0][0].text


def test_merge_keeps_existing_scheduled_ordering() -> None:
    canonical = _canonical_row(
        publication_id=42,
        scheduled_at=BASE_TIME + timedelta(hours=1),
    )
    earlier = _timed_row(
        "cp_open_post:1",
        scheduled_at=BASE_TIME - timedelta(hours=1),
        text="earlier",
    )
    middle = _timed_row(
        "cp_open_post:2",
        scheduled_at=BASE_TIME,
        text="middle",
    )

    merged = merge_timed_content_plan_rows([middle, earlier], [canonical])

    assert [row[0].text for row in merged] == [
        "earlier",
        "middle",
        canonical.buttons[0].text,
    ]


def test_queued_parent_behavior_remains_legacy_and_unmodified() -> None:
    queued_callback = "cp_open_post:9001"
    queued = _timed_row(
        queued_callback,
        scheduled_at=BASE_TIME,
        text="queued parent row",
    )

    merged = merge_timed_content_plan_rows([queued], [])

    assert len(merged) == 1
    assert merged[0][0].callback_data == queued_callback
    assert merged[0][0].text == "queued parent row"
