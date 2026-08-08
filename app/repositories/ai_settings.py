from sqlalchemy import select, or_, cast, String, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.url_security import validate_public_http_url
from app.domain.models import AIPreset, ChannelAISettings, AISource


class AIPresetsRepo:
    """Репозиторий для работы с пресетами промптов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, preset_id: int) -> AIPreset | None:
        res = await self.session.execute(
            select(AIPreset).where(AIPreset.id == preset_id)
        )
        return res.scalars().first()

    async def get_by_code(self, code: str) -> AIPreset | None:
        res = await self.session.execute(select(AIPreset).where(AIPreset.code == code))
        return res.scalars().first()

    async def list_active(self) -> list[AIPreset]:
        res = await self.session.execute(
            select(AIPreset).where(AIPreset.is_active.is_(True)).order_by(AIPreset.id)
        )
        return list(res.scalars().all())

    async def create(
        self,
        code: str,
        title: str,
        system_prompt: str,
        user_template: str,
        description: str | None = None,
        rules: dict | None = None,
        defaults: dict | None = None,
    ) -> AIPreset:
        preset = AIPreset(
            code=code,
            title=title,
            description=description,
            system_prompt=system_prompt,
            user_template=user_template,
            rules=rules or {},
            defaults=defaults or {},
            is_active=True,
        )
        self.session.add(preset)
        await self.session.commit()
        await self.session.refresh(preset)
        return preset


class ChannelAISettingsRepo:
    """Репозиторий для настроек ИИ канала/чата."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_channel_id(self, channel_id: int) -> ChannelAISettings | None:
        res = await self.session.execute(
            select(ChannelAISettings).where(ChannelAISettings.channel_id == channel_id)
        )
        return res.scalars().first()

    async def get_or_create(self, channel_id: int) -> ChannelAISettings:
        """Получить или создать настройки ИИ для канала."""
        settings = await self.get_by_channel_id(channel_id)
        if not settings:
            settings = ChannelAISettings(
                channel_id=channel_id,
                enabled=False,
                model="openai/gpt-4o-mini",
                temperature=0.7,
                top_p=0.9,
                max_tokens=2000,
                tone="friendly",
                length="medium",
                emoji_level=1,
                lang="ru",
                hashtags_enabled=True,
                hashtags_count=3,
                cta_enabled=True,
                links_allowed=True,
                utm_enabled=False,
                moderation_enabled=False,
                media_generation_enabled=False,
                ab_test_enabled=False,
                ab_variants_count=1,
                tokens_used_day=0,
                tokens_used_month=0,
            )
            self.session.add(settings)
            await self.session.commit()
            await self.session.refresh(settings)
        return settings

    async def update_enabled(self, channel_id: int, enabled: bool) -> bool:
        settings = await self.get_or_create(channel_id)
        settings.enabled = enabled
        await self.session.commit()
        return True

    async def update_preset(self, channel_id: int, preset_id: int | None) -> bool:
        settings = await self.get_or_create(channel_id)
        settings.preset_id = preset_id
        await self.session.commit()
        return True

    async def update_custom_prompt(
        self, channel_id: int, system_prompt: str | None, user_template: str | None
    ) -> bool:
        settings = await self.get_or_create(channel_id)
        settings.custom_prompt = system_prompt
        settings.user_prompt_template = user_template
        await self.session.commit()
        return True

    async def update_params(self, channel_id: int, **kwargs) -> bool:
        """Обновить произвольные параметры настроек ИИ."""
        settings = await self.get_or_create(channel_id)
        for key, value in kwargs.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        await self.session.commit()
        return True

    async def update_forbidden_words(self, channel_id: int, words: list[str]) -> bool:
        settings = await self.get_or_create(channel_id)
        settings.forbidden_words = words
        await self.session.commit()
        return True

    async def increment_tokens(self, channel_id: int, tokens: int) -> None:
        """Инкремент счётчиков использования токенов."""
        settings = await self.get_or_create(channel_id)
        settings.tokens_used_day += tokens
        settings.tokens_used_month += tokens
        await self.session.commit()


class AISourcesRepo:
    """Репозиторий для источников контента (RSS, URL, TG)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    async def _validate_source_value(source_type: str, source_value: str) -> None:
        if (source_type or "").lower().strip() in {"url", "rss"}:
            await validate_public_http_url((source_value or "").strip())

    async def list_by_channel(self, channel_id: int) -> list[AISource]:
        res = await self.session.execute(
            select(AISource)
            .where(AISource.channel_id == channel_id)
            .order_by(AISource.id)
        )
        items = list(res.scalars().all())

        # Existing rows predate URL validation. Fail closed: unsafe/unresolvable
        # URL/RSS sources are disabled before any caller can fetch them.
        changed = False
        for source in items:
            if not source.enabled:
                continue
            try:
                await self._validate_source_value(source.source_type, source.source_value)
            except Exception:
                source.enabled = False
                changed = True
        if changed:
            await self.session.commit()
        return items

    async def get_by_id(self, source_id: int) -> AISource | None:
        res = await self.session.execute(
            select(AISource).where(AISource.id == source_id)
        )
        return res.scalars().first()

    async def create(
        self,
        channel_id: int,
        source_type: str,
        source_value: str,
        mode: str = "summary",
        citation_enabled: bool = True,
    ) -> AISource:
        await self._validate_source_value(source_type, source_value)
        source = AISource(
            channel_id=channel_id,
            source_type=source_type,
            source_value=source_value,
            mode=mode,
            enabled=True,
            citation_enabled=citation_enabled,
        )
        self.session.add(source)
        await self.session.commit()
        await self.session.refresh(source)
        return source

    async def list_telegram_matches(
        self, chat_id: int | None = None, username: str | None = None
    ) -> list[AISource]:
        """Найти все включённые источники типа telegram, соответствующие chat_id или @username.
        username хранится в source_value как строка вида "@name".
        """
        base = [AISource.enabled.is_(True), AISource.source_type == "telegram"]
        if not chat_id and not username:
            return []
        match = None
        if chat_id and username:
            match = or_(
                func.lower(AISource.source_value) == func.lower(username),
                AISource.source_value == cast(str(chat_id), String),
                # поддержка сохранённых значений в форме ссылок
                func.lower(AISource.source_value)
                == func.lower(func.concat("https://t.me/", cast(username, String))),
            )
        elif username:
            match = or_(
                func.lower(AISource.source_value) == func.lower(username),
                func.lower(AISource.source_value)
                == func.lower(func.concat("https://t.me/", cast(username, String))),
            )
        else:
            match = AISource.source_value == cast(str(chat_id), String)
        res = await self.session.execute(select(AISource).where(*(base + [match])))
        return list(res.scalars().all())

    async def delete(self, source_id: int) -> bool:
        source = await self.get_by_id(source_id)
        if source:
            await self.session.delete(source)
            await self.session.commit()
            return True
        return False

    async def toggle_enabled(self, source_id: int) -> bool:
        source = await self.get_by_id(source_id)
        if source:
            if not source.enabled:
                await self._validate_source_value(source.source_type, source.source_value)
            source.enabled = not source.enabled
            await self.session.commit()
            return True
        return False

    async def toggle_citation(self, source_id: int) -> bool:
        source = await self.get_by_id(source_id)
        if source:
            source.citation_enabled = not source.citation_enabled
            await self.session.commit()
            return True
        return False

    async def update_mode(self, source_id: int, mode: str) -> bool:
        source = await self.get_by_id(source_id)
        if source:
            source.mode = mode
            await self.session.commit()
            return True
        return False

    async def update_value(
        self,
        source_id: int,
        source_type: str | None = None,
        source_value: str | None = None,
    ) -> bool:
        source = await self.get_by_id(source_id)
        if source:
            effective_type = source_type if source_type is not None else source.source_type
            effective_value = (
                source_value if source_value is not None else source.source_value
            )
            await self._validate_source_value(effective_type, effective_value)
            if source_type is not None:
                source.source_type = source_type
            if source_value is not None:
                source.source_value = source_value
            await self.session.commit()
            return True
        return False

    async def list_all_telegram_enabled(self) -> list[AISource]:
        res = await self.session.execute(
            select(AISource).where(
                AISource.source_type == "telegram", AISource.enabled.is_(True)
            )
        )
        return list(res.scalars().all())