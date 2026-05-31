from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import ChannelSettings
from app.repositories.base import BaseRepository


class ChannelSettingsRepo(BaseRepository[ChannelSettings]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, ChannelSettings)

    async def get_by_channel_id(self, channel_id: int) -> ChannelSettings | None:
        res = await self.session.execute(
            select(ChannelSettings).where(ChannelSettings.channel_id == channel_id)
        )
        return res.scalars().first()

    async def update_timezone(self, channel_id: int, tz_code: str) -> bool:
        st = await self.get_by_channel_id(channel_id)
        if not st:
            st = ChannelSettings(
                channel_id=channel_id, autosign=None, split_rules=[], filters={}
            )
            self.session.add(st)
        if st.filters is None:
            st.filters = {}
        st.filters["tz"] = tz_code
        await self.session.commit()
        return True

    # Сервисные флаги (вкл/выкл удаления служебных сообщений и т.п.)
    async def get_service_flags(self, channel_id: int) -> dict:
        st = await self.get_by_channel_id(channel_id)
        return dict(getattr(st, "filters", {}) or {}).get("service", {}) if st else {}

    async def set_service_flag(self, channel_id: int, key: str, value: bool) -> None:
        st = await self.get_by_channel_id(channel_id)
        if not st:
            st = ChannelSettings(
                channel_id=channel_id, autosign=None, split_rules=[], filters={}
            )
            self.session.add(st)
        if st.filters is None:
            st.filters = {}
        service = dict(st.filters.get("service", {}))
        service[key] = 1 if value else 0
        st.filters["service"] = service
        await self.session.commit()

    async def update_autosign(self, channel_id: int, text: str | None) -> None:
        st = await self.get_by_channel_id(channel_id)
        if not st:
            st = ChannelSettings(
                channel_id=channel_id, autosign=None, split_rules=[], filters={}
            )
            self.session.add(st)
        st.autosign = text or None
        await self.session.commit()
