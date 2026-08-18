from __future__ import annotations

from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
)


def canonical_publication_delivery_nonrepeat_plain_started() -> bool:
    """Return whether the successfully started canonical primary owns plain non-repeat.

    Plain/silent delivery has no auxiliary runtime dependency beyond the canonical
    primary itself. Keeping a profile-specific fact gives scheduler admission an exact
    capability name without inferring any future pin/forward/delete composition.
    """

    return canonical_publication_delivery_primary_started()
