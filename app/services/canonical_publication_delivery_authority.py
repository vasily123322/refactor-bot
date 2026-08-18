from __future__ import annotations

import weakref


_primary_worker_ref: weakref.ReferenceType[object] | None = None


def canonical_publication_delivery_primary_started() -> bool:
    """Return whether the successfully started canonical primary is still referenced."""

    return _primary_worker_ref is not None and _primary_worker_ref() is not None


def set_canonical_publication_delivery_primary_worker(worker: object | None) -> None:
    """Publish canonical primary authority from concrete successful start/stop boundaries."""

    global _primary_worker_ref
    _primary_worker_ref = None if worker is None else weakref.ref(worker)
