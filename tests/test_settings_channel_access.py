import asyncio
from types import SimpleNamespace

import pytest

from app.core.settings_channel_access import (
    _direct_channel_id_from_callback,
    _grab_source_ref_from_callback,
    _post_task_id_from_callback,
    _resolve_channel_target,
)
from app.domain.models import GrabSource, PostTask


@pytest.mark.parametrize(
    ("callback_data", "expected_channel_id"),
    [
        ("channels_settings_12", 12),
        ("settings_post_12", 12),
        ("post_replace_autosign_12", 12),
        ("post_avtostring_12", 12),
        ("post_split_12", 12),
        ("split_add_12", 12),
        ("split_remove_12", 12),
        ("settings_application_12", 12),
        ("app_mode_auto_12", 12),
        ("app_mode_manual_12", 12),
        ("settings_graber_12", 12),
        ("graber_add_12", 12),
        ("graber_remove_12", 12),
        ("settings_delete_12", 12),
        ("bot_manage_12", 12),
        ("bot_manage_require_dm_preset:12", 12),
        ("open_service_menu_channel:12", 12),
        ("service_toggle_channel:join_requests:12", 12),
        ("tz_pick:12:Europe/London", 12),
        ("tz_set_offset:12:180", 12),
        ("cp_pick_channel_12", 12),
        ("cp_day_prev:12:2026-08-10", 12),
        ("cp_day_next:12:2026-08-10", 12),
        ("cp_open_cal:12:2026-08-10", 12),
        ("cp_calendar_back:12:2026-08-10", 12),
        ("cp_month_prev:12:2026-08-10", 12),
        ("cp_month_next:12:2026-08-10", 12),
        ("cp_pick_day:12:2026-08-10", 12),
        ("settings_ui_toggle:ad_posts", None),
        ("tz_set_global:180", None),
        ("unrelated_12", None),
    ],
)
def test_direct_channel_callback_parser(
    callback_data: str, expected_channel_id: int | None
) -> None:
    assert _direct_channel_id_from_callback(callback_data) == expected_channel_id


@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("graber_src_12_44", (44, 12)),
        ("graber_del_44_12", (44, 12)),
        ("graber_flag_44_forward", (44, None)),
        ("settings_graber_12", None),
    ],
)
def test_grab_source_callback_parser(
    callback_data: str, expected: tuple[int, int | None] | None
) -> None:
    assert _grab_source_ref_from_callback(callback_data) == expected


@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("cp_open_post:44:2026-08-10", 44),
        ("cp_edit_post:44:2026-08-10", 44),
        ("cp_delete_post:44:2026-08-10", 44),
        ("cp_repeat_off:44", 44),
        ("cp_open_pub:44:2026-08-10", None),
        ("cp_open_post:44:not-a-date", None),
        ("settings_post_44", None),
    ],
)
def test_post_task_callback_parser(callback_data: str, expected: int | None) -> None:
    assert _post_task_id_from_callback(callback_data) == expected


class _FakeSession:
    def __init__(
        self,
        source_channel_id: int | None = None,
        post_task_channel_id: int | None = None,
    ):
        self.source_channel_id = source_channel_id
        self.post_task_channel_id = post_task_channel_id

    async def get(self, model, object_id: int):
        if model is GrabSource:
            if self.source_channel_id is None or object_id != 44:
                return None
            return SimpleNamespace(target_channel_id=self.source_channel_id)
        if model is PostTask:
            if self.post_task_channel_id is None or object_id != 44:
                return None
            return SimpleNamespace(channel_id=self.post_task_channel_id)
        return None


def test_grab_source_target_uses_database_channel() -> None:
    recognized, channel_id = asyncio.run(
        _resolve_channel_target(_FakeSession(source_channel_id=12), "graber_flag_44_forward")
    )
    assert recognized is True
    assert channel_id == 12


def test_grab_source_rejects_claimed_channel_mismatch() -> None:
    recognized, channel_id = asyncio.run(
        _resolve_channel_target(_FakeSession(source_channel_id=99), "graber_del_44_12")
    )
    assert recognized is True
    assert channel_id is None


def test_missing_grab_source_fails_closed() -> None:
    recognized, channel_id = asyncio.run(
        _resolve_channel_target(_FakeSession(), "graber_src_12_44")
    )
    assert recognized is True
    assert channel_id is None


def test_content_plan_post_task_target_uses_database_channel() -> None:
    recognized, channel_id = asyncio.run(
        _resolve_channel_target(
            _FakeSession(post_task_channel_id=12),
            "cp_delete_post:44:2026-08-10",
        )
    )
    assert recognized is True
    assert channel_id == 12


def test_missing_content_plan_post_task_fails_closed() -> None:
    recognized, channel_id = asyncio.run(
        _resolve_channel_target(_FakeSession(), "cp_open_post:44:2026-08-10")
    )
    assert recognized is True
    assert channel_id is None


def test_canonical_publication_callback_stays_on_canonical_owner_boundary() -> None:
    recognized, channel_id = asyncio.run(
        _resolve_channel_target(_FakeSession(), "cp_open_pub:44:2026-08-10")
    )
    assert recognized is False
    assert channel_id is None
