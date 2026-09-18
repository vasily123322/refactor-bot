from __future__ import annotations

from pathlib import Path

from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import PostTask
from app.domain.scheduler import SchedulerTaskLease


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_p7_startup_has_only_canonical_publication_runtime() -> None:
    source = _source("app/bot/dispatcher.py")

    assert "CanonicalRepeatContinuationWorker" in source
    assert "CanonicalPublicationDeliveryRecoveryWorker" in source
    assert "start_canonical_publication_safe_repeat_primary_if_enabled" in source

    assert "canonical_repeat_continuation_scheduler" not in source
    assert "SchedulerRecoveryWorker" not in source
    assert "PublicationReconcilerWorker" not in source
    assert "PostTaskRetentionWorker" not in source
    assert "scheduler = Scheduler(" not in source
    assert "scheduler recovery" not in source
    assert "publication reconciler" not in source


def test_p7_canonical_primary_executes_without_legacy_handoff_wrapper() -> None:
    source = _source(
        "app/services/canonical_publication_safe_repeat_runtime_control.py"
    )
    assert "executor=runtime.executor" in source
    assert "CanonicalPublicationRepeatHandoffExecutor" not in source
    assert "legacy_post_task_id" not in source


def test_p7_live_canonical_paths_do_not_load_or_mutate_posttask() -> None:
    paths = [
        "app/services/document_posting.py",
        "app/services/planner.py",
        "app/services/queued_canonical_publication_edit.py",
        "app/services/publication_autodelete_views.py",
        "app/workers/publication_autodelete_views_pin_forward.py",
    ]
    for path in paths:
        source = _source(path)
        assert "from app.domain.models import PostTask" not in source
        assert "select(PostTask)" not in source
        assert "update(PostTask)" not in source
        assert "LegacyMixedTimeViewsAutodeleteObserver" not in source


def test_p7_retired_runtime_modules_are_deleted() -> None:
    retired = [
        "app/workers/scheduler.py",
        "app/workers/reliable_scheduler.py",
        "app/workers/publication_scheduler.py",
        "app/workers/canonical_scheduler.py",
        "app/workers/canonical_recovery_scheduler.py",
        "app/workers/canonical_repeat_continuation_scheduler.py",
        "app/workers/scheduler_recovery.py",
        "app/workers/publication_reconciler.py",
        "app/services/scheduler_task_lease.py",
        "app/services/scheduler_recovery.py",
        "app/services/legacy_runtime_drain.py",
        "app/services/legacy_mixed_time_views_autodelete.py",
        "app/services/canonical_publication_repeat_handoff_executor.py",
        "app/services/canonical_repeat_recovery_shadow.py",
        "app/services/canonical_repeat_boot_recovery_shadow.py",
    ]
    assert [path for path in retired if (ROOT / path).exists()] == []


def test_p7_preserves_p8_schema_and_durable_evidence_models() -> None:
    assert PostTask.__tablename__ == "post_tasks"
    assert SchedulerTaskLease.__tablename__ == "scheduler_task_leases"
    assert LegacyTimeViewsDeleteAction.__tablename__ == "legacy_time_views_delete_actions"
