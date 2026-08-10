from __future__ import annotations

from typing import Protocol


class RuntimeConfigurationError(RuntimeError):
    """Raised when an explicitly enabled runtime combination is unsafe."""


class _RetentionRuntimeSettings(Protocol):
    post_task_retention_enabled: bool
    post_task_retention_successful_enabled: bool
    post_task_retention_successful_pending_autodelete_enabled: bool
    publication_autodelete_worker_enabled: bool
    publication_autodelete_views_worker_enabled: bool


def _pending_retention_active(settings: _RetentionRuntimeSettings) -> bool:
    return bool(
        settings.post_task_retention_enabled
        and settings.post_task_retention_successful_enabled
        and settings.post_task_retention_successful_pending_autodelete_enabled
    )


def validate_runtime_configuration(settings: _RetentionRuntimeSettings) -> None:
    """Reject destructive retention unless configured canonical executors are safe."""

    if (
        settings.post_task_retention_enabled
        and settings.post_task_retention_successful_enabled
        and not settings.publication_autodelete_worker_enabled
    ):
        raise RuntimeConfigurationError(
            "POST_TASK_RETENTION_SUCCESSFUL_ENABLED requires "
            "PUBLICATION_AUTODELETE_WORKER_ENABLED when "
            "POST_TASK_RETENTION_ENABLED is enabled"
        )

    if _pending_retention_active(settings):
        if not settings.publication_autodelete_worker_enabled:
            raise RuntimeConfigurationError(
                "POST_TASK_RETENTION_SUCCESSFUL_PENDING_AUTODELETE_ENABLED requires "
                "PUBLICATION_AUTODELETE_WORKER_ENABLED"
            )
        if not settings.publication_autodelete_views_worker_enabled:
            raise RuntimeConfigurationError(
                "POST_TASK_RETENTION_SUCCESSFUL_PENDING_AUTODELETE_ENABLED requires "
                "PUBLICATION_AUTODELETE_VIEWS_WORKER_ENABLED"
            )


def validate_retention_executor_availability(
    settings: _RetentionRuntimeSettings,
    *,
    publication_autodelete_worker_started: bool,
    publication_autodelete_views_worker_started: bool,
) -> None:
    """Fail closed if configured pending-retirement executors did not actually start."""

    if not _pending_retention_active(settings):
        return
    if not publication_autodelete_worker_started:
        raise RuntimeConfigurationError(
            "pending successful PostTask retention requires a started canonical "
            "publication autodelete worker"
        )
    if not publication_autodelete_views_worker_started:
        raise RuntimeConfigurationError(
            "pending successful PostTask retention requires a started canonical "
            "views autodelete worker"
        )
