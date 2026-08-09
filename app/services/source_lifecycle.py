from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AISource
from app.domain.sources.models import SourceConnector
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion_lease import SourceIngestionLeaseService


class SourceLifecycleError(RuntimeError):
    pass


class SourceLifecycleBusy(SourceLifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class SourceLifecyclePatch:
    enabled: bool | None = None
    mode: str | None = None
    citation_enabled: bool | None = None
    reuse_policy: str | None = None


_ALLOWED_MODES = frozenset({"summary", "rewrite"})
_ALLOWED_REUSE_POLICIES = frozenset(
    {
        "reference_only",
        "summarize",
        "quote_with_attribution",
        "rewrite_with_attribution",
        "mirror_authorized",
    }
)


class SourceLifecycleService:
    """Update normalized source state and editable legacy AI projection atomically."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = SourcesRepo(session)

    async def update(
        self,
        *,
        channel_id: int,
        connector_id: int,
        patch: SourceLifecyclePatch,
    ) -> SourceConnector:
        connector = await self.repo.get_connector_for_channel(connector_id, channel_id)
        if connector is None:
            raise SourceLifecycleError("source not found")

        # Acquire the same connector lease used by worker/manual ingestion. A simple
        # active-status read is vulnerable to a check-then-mutate race: ingestion can
        # start after the read but before this lifecycle update commits.
        lease_service = SourceIngestionLeaseService(self.session)
        lease = await lease_service.acquire(
            connector_id=int(connector.id),
            holder="lifecycle",
            ttl_seconds=30,
        )
        if lease is None:
            raise SourceLifecycleBusy("source ingestion is running")

        try:
            # Legacy GrabSource has no persisted enabled/mode/citation state. Such rows
            # are mirrored into Sources v2 for observability only; accepting lifecycle
            # writes here would make Studio claim a change that the legacy grab runtime
            # cannot persist or honor.
            if (
                connector.legacy_grab_source_id is not None
                and connector.legacy_ai_source_id is None
            ):
                raise SourceLifecycleError("legacy grab source is read-only")

            if patch.mode is not None and patch.mode not in _ALLOWED_MODES:
                raise SourceLifecycleError("unsupported source mode")
            if (
                patch.reuse_policy is not None
                and patch.reuse_policy not in _ALLOWED_REUSE_POLICIES
            ):
                raise SourceLifecycleError("unsupported reuse policy")

            if patch.enabled is not None:
                connector.enabled = bool(patch.enabled)
            if patch.mode is not None:
                connector.mode = patch.mode
            if patch.citation_enabled is not None:
                connector.citation_enabled = bool(patch.citation_enabled)
            if patch.reuse_policy is not None:
                connector.reuse_policy = patch.reuse_policy

            legacy_ai = (
                await self.session.get(AISource, int(connector.legacy_ai_source_id))
                if connector.legacy_ai_source_id is not None
                else None
            )
            if legacy_ai is not None:
                if patch.enabled is not None:
                    legacy_ai.enabled = bool(patch.enabled)
                if patch.mode is not None:
                    legacy_ai.mode = patch.mode
                if patch.citation_enabled is not None:
                    legacy_ai.citation_enabled = bool(patch.citation_enabled)

            await self.session.commit()
            await self.session.refresh(connector)
            return connector
        except Exception:
            # Ensure lease cleanup below cannot commit partially-mutated ORM state if
            # anything fails before/during the lifecycle commit.
            await self.session.rollback()
            raise
        finally:
            try:
                await lease_service.release(lease)
            except Exception:
                # A short TTL is the fail-safe if cleanup itself loses DB access.
                await self.session.rollback()
