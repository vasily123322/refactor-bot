from __future__ import annotations

import weakref


_primary_worker_ref: weakref.ReferenceType[object] | None = None
_time_autodelete_available = False


def canonical_publication_delivery_primary_started() -> bool:
    """Return whether the successfully started canonical primary is still referenced."""

    return _primary_worker_ref is not None and _primary_worker_ref() is not None


def canonical_publication_delivery_time_autodelete_started() -> bool:
    """Return the started primary fact for canonical non-repeat time autodelete."""

    return canonical_publication_delivery_primary_started() and _time_autodelete_available


def set_canonical_publication_delivery_primary_worker(
    worker: object | None,
    *,
    time_autodelete_available: bool = False,
) -> None:
    """Publish primary authority only with capabilities proven at successful startup."""

    global _primary_worker_ref, _time_autodelete_available
    _primary_worker_ref = None if worker is None else weakref.ref(worker)
    _time_autodelete_available = bool(
        worker is not None and time_autodelete_available
    )
