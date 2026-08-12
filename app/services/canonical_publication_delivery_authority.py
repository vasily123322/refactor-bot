from __future__ import annotations


_primary_started = False


def canonical_publication_delivery_primary_started() -> bool:
    """Return the process-local fact that canonical primary successfully started."""

    return _primary_started


def set_canonical_publication_delivery_primary_started(started: bool) -> None:
    """Publish canonical primary liveness only from concrete start/stop boundaries."""

    global _primary_started
    _primary_started = bool(started)
