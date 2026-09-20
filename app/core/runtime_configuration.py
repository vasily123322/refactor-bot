from __future__ import annotations

from typing import Protocol


class RuntimeConfigurationError(RuntimeError):
    """Raised when an explicitly enabled runtime combination is unsafe."""


class _RuntimeSettings(Protocol):
    canonical_repeat_shadow_planning_enabled: bool
    canonical_repeat_successful_planning_enabled: bool
    canonical_repeat_overdue_recovery_shadow_enabled: bool
    canonical_repeat_overdue_recovery_planning_enabled: bool
    canonical_repeat_boot_recovery_shadow_enabled: bool
    canonical_repeat_boot_recovery_planning_enabled: bool


def validate_runtime_configuration(settings: _RuntimeSettings) -> None:
    """Reject explicitly enabled canonical runtime combinations that are not proven safe."""

    if (
        settings.canonical_repeat_successful_planning_enabled
        and not settings.canonical_repeat_shadow_planning_enabled
    ):
        raise RuntimeConfigurationError(
            "CANONICAL_REPEAT_SUCCESSFUL_PLANNING_ENABLED requires "
            "CANONICAL_REPEAT_SHADOW_PLANNING_ENABLED"
        )

    if (
        settings.canonical_repeat_overdue_recovery_planning_enabled
        and not settings.canonical_repeat_overdue_recovery_shadow_enabled
    ):
        raise RuntimeConfigurationError(
            "CANONICAL_REPEAT_OVERDUE_RECOVERY_PLANNING_ENABLED requires "
            "CANONICAL_REPEAT_OVERDUE_RECOVERY_SHADOW_ENABLED"
        )

    if (
        settings.canonical_repeat_boot_recovery_planning_enabled
        and not settings.canonical_repeat_boot_recovery_shadow_enabled
    ):
        raise RuntimeConfigurationError(
            "CANONICAL_REPEAT_BOOT_RECOVERY_PLANNING_ENABLED requires "
            "CANONICAL_REPEAT_BOOT_RECOVERY_SHADOW_ENABLED"
        )
