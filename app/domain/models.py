from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import (
    BigInteger,
    Integer,
    String,
    Boolean,
    ForeignKey,
    DateTime,
    Text,
    JSON,
    func,
    UniqueConstraint,
    Index,
)
from app.core.db import Base
from app.domain.mixins import (
    TimestampHelpersMixin,
    OwnerHelpersMixin,
    ActivatableHelpersMixin,
)


class CreatedAtMixin:
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Client(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(128))
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    ui_settings: Mapped[dict | None] = mapped_column(JSON, default=dict)
    last_channel_id: Mapped[int | None] = mapped_column(Integer)

    channels: Mapped[list[Channel]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Channel(
    CreatedAtMixin,
    OwnerHelpersMixin,
    ActivatableHelpersMixin,
    TimestampHelpersMixin,
    Base,
):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    owner_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    owner: Mapped[Client] = relationship(back_populates="channels")
    settings: Mapped[ChannelSettings] = relationship(
        back_populates="channel", uselist=False, cascade="all, delete-orphan"
    )
    grab_sources: Mapped[list[GrabSource]] = relationship(
        back_populates="target_channel", cascade="all, delete-orphan"
    )


class ChannelSettings(TimestampHelpersMixin, Base):
    __tablename__ = "channel_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), unique=True
    )
    autosign: Mapped[str | None] = mapped_column(Text)
    split_rules: Mapped[list[str] | None] = mapped_column(JSON)
    filters: Mapped[dict] = mapped_column(
        JSON,
        default={
            "url": 1,
            "audio": 1,
            "video": 1,
            "photo": 1,
            "text": 1,
            "animation": 1,
        },
    )

    channel: Mapped[Channel] = relationship(back_populates="settings")


class GrabSource(TimestampHelpersMixin, Base):
    __tablename__ = "grab_sources"
    __table_args__ = (
        UniqueConstraint(
            "source_chat_id", "target_channel_id", name="uq_grab_source_target"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    filter_flags: Mapped[dict] = mapped_column(
        JSON,
        default={
            "url": 0,
            "audio": 1,
            "video": 1,
            "photo": 1,
            "text": 1,
            "animation": 1,
        },
    )

    target_channel: Mapped[Channel] = relationship(back_populates="grab_sources")


class Application(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "applications"
    __table_args__ = (
        Index("ix_app_channel_user", "channel_id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[int] = mapped_column(Integer, default=0)  # 0 auto, 1 manual


class Subscriber(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "subscribers"
    __table_args__ = (
        Index("ix_sub_channel_user", "channel_id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(128))
    tags: Mapped[list[str] | None] = mapped_column(JSON)


class PostTask(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "post_tasks"
    __table_args__ = (Index("ix_post_dedupe", "dedupe_key", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(32), default="pending")
    payload: Mapped[dict] = mapped_column(JSON)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    scheduled_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


# --- AI / Нейропостинг ---


class AIPreset(CreatedAtMixin, TimestampHelpersMixin, Base):
    """Готовые пресеты промптов для генерации контента."""

    __tablename__ = "ai_presets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    user_template: Mapped[str] = mapped_column(Text)
    rules: Mapped[dict] = mapped_column(JSON, default={})
    defaults: Mapped[dict] = mapped_column(JSON, default={})
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ChannelAISettings(CreatedAtMixin, TimestampHelpersMixin, Base):
    """Настройки ИИ для конкретного канала/чата."""

    __tablename__ = "channel_ai_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), unique=True
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    model: Mapped[str] = mapped_column(String(128), default="openai/gpt-4o-mini")
    temperature: Mapped[float] = mapped_column(default=0.7)
    top_p: Mapped[float] = mapped_column(default=0.9)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2000)

    preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_presets.id", ondelete="SET NULL")
    )
    custom_prompt: Mapped[str | None] = mapped_column(Text)
    user_prompt_template: Mapped[str | None] = mapped_column(Text)

    tone: Mapped[str] = mapped_column(String(64), default="friendly")
    length: Mapped[str] = mapped_column(String(32), default="medium")
    emoji_level: Mapped[int] = mapped_column(Integer, default=1)
    lang: Mapped[str] = mapped_column(String(8), default="ru")

    hashtags_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    hashtags_count: Mapped[int] = mapped_column(Integer, default=3)
    cta_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    links_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    utm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    moderation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    forbidden_words: Mapped[list[str] | None] = mapped_column(JSON)
    filters: Mapped[dict] = mapped_column(JSON, default={})

    media_generation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    ab_test_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ab_variants_count: Mapped[int] = mapped_column(Integer, default=1)

    tokens_limit_day: Mapped[int | None] = mapped_column(Integer)
    tokens_limit_month: Mapped[int | None] = mapped_column(Integer)
    tokens_used_day: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_month: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    preset: Mapped[AIPreset | None] = relationship()


class AISource(CreatedAtMixin, TimestampHelpersMixin, Base):
    """Источники для рерайта/саммари (RSS, URL, TG-канал)."""

    __tablename__ = "ai_sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(String(32))
    source_value: Mapped[str] = mapped_column(String(512))
    mode: Mapped[str] = mapped_column(String(32), default="summary")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    citation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# --- AI Conversations (память диалогов) ---


class AIConversation(TimestampHelpersMixin, Base):
    __tablename__ = "ai_conversations"
    __table_args__ = (
        Index("ix_conv_user_prompt", "user_id", "prompt_key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    prompt_key: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    last_summary: Mapped[str | None] = mapped_column(Text)
    summary_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AIConversationMessage(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "ai_conversation_messages"
    __table_args__ = (
        Index("ix_conv_messages_conv_time", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("ai_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # system|user|assistant
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict | None] = mapped_column(JSON)


# --- Custom System Prompts ---


class AICustomSystemPrompt(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "ai_custom_system_prompts"
    __table_args__ = (Index("ix_csp_channel_active", "channel_id", "is_active"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


# --- External Join Management ---


class ExternalBot(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "external_bots"
    __table_args__ = (UniqueConstraint("bot_user_id", name="uq_extbot_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(Text)
    bot_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    bot_username: Mapped[str | None] = mapped_column(String(64))
    owner_client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ModLog(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "mod_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(
        String(32)
    )  # approve|reject|mode_change|anti_raid
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    moderator_id: Mapped[int | None] = mapped_column(BigInteger)
    meta: Mapped[dict | None] = mapped_column(JSON)


class ChannelBot(TimestampHelpersMixin, Base):
    __tablename__ = "channel_bots"
    __table_args__ = (
        UniqueConstraint("channel_id", name="uq_channel_bot_one"),
        Index("ix_channelbot_extbot", "external_bot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    external_bot_id: Mapped[int] = mapped_column(
        ForeignKey("external_bots.id", ondelete="CASCADE")
    )
    mode: Mapped[int] = mapped_column(Integer, default=0)  # 0=auto, 1=delayed, 2=manual
    welcome_text: Mapped[str | None] = mapped_column(Text)
    farewell_text: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON)


class JoinRequest(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "join_requests"
    __table_args__ = (Index("ix_joinreq_channel_user", "channel_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending|approved|rejected
    # Поля для челенджа /start
    challenge_type: Mapped[str | None] = mapped_column(
        String(16)
    )  # simple|captcha|keyword
    challenge_payload: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    attempts_left: Mapped[int | None] = mapped_column(Integer, default=3)
    invite_link: Mapped[str | None] = mapped_column(String(512))


# --- Admin / Moderation ---


class AdminConfig(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "admin_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    log_chat_id: Mapped[int | None] = mapped_column(BigInteger)


class BannedChat(CreatedAtMixin, TimestampHelpersMixin, Base):
    __tablename__ = "banned_chats"
    __table_args__ = (
        UniqueConstraint("tg_chat_id", name="uq_banned_chat"),
        Index("ix_banned_chat", "tg_chat_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
