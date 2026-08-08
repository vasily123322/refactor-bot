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
            )
            row.kind = str(source.source_type)
            row.value = str(source.source_value)
            row.enabled = bool(source.enabled)
            row.mode = str(source.mode or "summary")
            row.citation_enabled = bool(source.citation_enabled)
            row.reuse_policy = "reference_only"
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
                    legacy_grab_source_id=int(source.id),
                )
                self.session.add(row)
                changed += 1
            before = (row.value, row.enabled, dict(row.config or {}))
            row.kind = "telegram"
            row.value = str(source.source_chat_id)
            row.enabled = True
            row.mode = "mirror"
            # A legacy grab rule is mirrored for observability only. We do not infer
            # that the operator has republication rights from an old DB row.
            row.reuse_policy = "reference_only"
            row.config = {
                **dict(row.config or {}),
                "legacy_source": "grab_source",
                "filter_flags": dict(source.filter_flags or {}),
            }
            after = (row.value, row.enabled, dict(row.config or {}))
            if row.id is not None and before != after:
                changed += 1

        if changed:
            try:
                await self.session.commit()
            except Exception:
                await self.session.rollback()
                raise
        return changed
