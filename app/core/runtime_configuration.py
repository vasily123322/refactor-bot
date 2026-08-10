from __future__ import annotations

from typing import Protocol


class RuntimeConfigurationError(RuntimeError):
    """Raised when an explicitly enabled runtime combination is unsafe."""


class _RetentionRuntimeSettings(Protocol):
    post_task_retention_enabled: bool
    post_task_retention_successful_enabled: bool
    publication_autodelete_worker_enabled: bool


def validate_runtime_configuration(settings: _RetentionRuntimeSettings) -> None:
    """Reject destructive retention unless its canonical executor is available.

    Successful PostTask retirement can remove the legacy transport that still executes
    post-publication autodelete semantics. The canonical autodelete worker must therefore
    be explicitly enabled before successful retention is allowed to run.
    """

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
