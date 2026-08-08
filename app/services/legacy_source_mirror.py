from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AISource, GrabSource
from app.domain.sources.models import SourceConnector


class LegacySourceMirror:
    """Project legacy AI/grab sources into the normalized Sources v2 read model."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def sync_channel(self, channel_id: int) -> int:
        changed = 0
        ai_sources = list(
            (
                await self.session.execute(
                    select(AISource).where(AISource.channel_id == int(channel_id))
                )
            ).scalars().all()
        )
        grab_sources = list(
            (
                await self.session.execute(
                    select(GrabSource).where(GrabSource.target_channel_id == int(channel_id))
                )
            ).scalars().all()
        )

        for source in ai_sources:
            row = (
                await self.session.execute(
                    select(SourceConnector).where(
                        SourceConnector.legacy_ai_source_id == int(source.id)
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = SourceConnector(
                    channel_id=int(source.channel_id),
                    kind=str(source.source_type),
                    value=str(source.source_value),
                    reuse_policy="reference_only",
                    legacy_ai_source_id=int(source.id),
                )
                self.session.add(row)
                changed += 1
            before = (
                row.kind,
                row.value,
                row.enabled,
                row.mode,
                row.citation_enabled,
                dict(row.config or {}),
            )
            row.kind = str(source.source_type)
            row.value = str(source.source_value)
            row.enabled = bool(source.enabled)
            row.mode = str(source.mode or "summary")
            row.citation_enabled = bool(source.citation_enabled)
            # reuse_policy is owned by Sources v2. Legacy AISource has no equivalent
            # field, so sync must never erase an explicit policy chosen in Studio.
            row.config = {
                **dict(row.config or {}),
                "legacy_source": "ai_source",
            }
            after = (
                row.kind,
                row.value,
                row.enabled,
                row.mode,
                row.citation_enabled,
                dict(row.config or {}),
            )
            if row.id is not None and before != after:
                changed += 1

        for source in grab_sources:
            row = (
                await self.session.execute(
                    select(SourceConnector).where(
                        SourceConnector.legacy_grab_source_id == int(source.id)
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = SourceConnector(
                    channel_id=int(source.target_channel_id),
                    kind="telegram",
                    value=str(source.source_chat_id),
                    enabled=True,
                    mode="mirror",
                    reuse_policy="reference_only",
                    legacy_grab_source_id=int(source.id),
                )
                self.session.add(row)
                changed += 1
            before = (
                row.value,
                row.enabled,
                row.mode,
                row.reuse_policy,
                dict(row.config or {}),
            )
            row.kind = "telegram"
            row.value = str(source.source_chat_id)
            # GrabSource has no persisted enabled/mode/citation controls. These rows
            # are observability mirrors, not editable Sources v2 lifecycle records.
            row.enabled = True
            row.mode = "mirror"
            row.reuse_policy = "reference_only"
            row.config = {
                **dict(row.config or {}),
                "legacy_source": "grab_source",
                "lifecycle_editable": False,
                "filter_flags": dict(source.filter_flags or {}),
            }
            after = (
                row.value,
                row.enabled,
                row.mode,
                row.reuse_policy,
                dict(row.config or {}),
            )
            if row.id is not None and before != after:
                changed += 1

        if changed:
            try:
                await self.session.commit()
            except Exception:
                await self.session.rollback()
                raise
        return changed
