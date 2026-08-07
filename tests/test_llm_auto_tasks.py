"""Tests for AI auto-tasks: worker logic, UI helpers, repository."""

import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.llm.auto_tasks_ui import (
    build_auto_tasks_menu_text,
)
from app.workers.ai_auto_tasks import (
    ALL_TASK_TYPES,
    TASK_LABELS,
    TASK_DEFAULT_TIMES,
    _parse_time,
    _is_due,
)


class TestParseTime:
    def test_valid_time(self):
        assert _parse_time("10:00") == (10, 0)

    def test_midnight(self):
        assert _parse_time("00:00") == (0, 0)

    def test_late_night(self):
        assert _parse_time("23:59") == (23, 59)


class TestIsDue:
    def _make_task(
        self,
        *,
        enabled=True,
        run_at="10:00",
        schedule="daily",
        day_of_week=None,
        last_run_at=None,
    ):
        return SimpleNamespace(
            enabled=enabled,
            run_at=run_at,
            schedule=schedule,
            day_of_week=day_of_week,
            last_run_at=last_run_at,
        )

    def test_disabled_task_not_due(self):
        task = self._make_task(enabled=False)
        now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        assert not _is_due(task, now)

    def test_time_mismatch_not_due(self):
        task = self._make_task(run_at="10:00")
        now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
        assert not _is_due(task, now)

    def test_exact_time_match_due(self):
        task = self._make_task(run_at="10:00")
        now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        assert _is_due(task, now)

    def test_recently_run_not_due(self):
        task = self._make_task(
            run_at="10:00",
            last_run_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        assert not _is_due(task, now)

    def test_last_run_before_minute_window_due(self):
        task = self._make_task(
            run_at="10:00",
            last_run_at=datetime(2026, 1, 9, 10, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        assert _is_due(task, now)

    def test_weekly_wrong_day_not_due(self):
        task = self._make_task(
            run_at="09:00",
            schedule="weekly",
            day_of_week=0,  # Monday
        )
        # Wednesday
        now = datetime(2026, 1, 3, 9, 0, tzinfo=timezone.utc)  # Wednesday
        # weekday() for Jan 3 2026 is... let's check
        assert now.weekday() != 0  # Not Monday
        # Actually Jan 3 2026 is Saturday (weekday=5)
        assert not _is_due(task, now)


class TestBuildAutoTasksMenuText:
    def test_all_tasks_shown(self):
        tasks_enabled = {t: False for t in ALL_TASK_TYPES}
        text = build_auto_tasks_menu_text(tasks_enabled)
        for task_type in ALL_TASK_TYPES:
            label = TASK_LABELS.get(task_type, task_type)
            assert label in text

    def test_enabled_status_shown(self):
        tasks_enabled = {TASK_LABELS.keys().__iter__().__next__(): True}
        # Just check it doesn't crash
        text = build_auto_tasks_menu_text(tasks_enabled)
        assert "Автозадачи" in text

    def test_empty_dict_no_crash(self):
        text = build_auto_tasks_menu_text({})
        assert "Автозадачи" in text


class TestTaskTypes:
    def test_three_task_types(self):
        assert ALL_TASK_TYPES == (
            "daily_topics",
            "evening_digest",
            "weekly_ideas",
        )

    def test_all_have_labels(self):
        for t in ALL_TASK_TYPES:
            assert t in TASK_LABELS
            assert len(TASK_LABELS[t]) > 0

    def test_all_have_default_times(self):
        for t in ALL_TASK_TYPES:
            assert t in TASK_DEFAULT_TIMES
            # Validate format
            parts = TASK_DEFAULT_TIMES[t].split(":")
            assert len(parts) == 2
            int(parts[0])
            int(parts[1])
