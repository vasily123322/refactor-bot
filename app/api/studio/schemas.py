from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StudioUserResponse(BaseModel):
    tg_user_id: int
    username: str | None
    full_name: str | None


class ChannelResponse(BaseModel):
    id: int
    tg_chat_id: int
    title: str | None
    is_active: bool


class ContentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    title: str | None = Field(default=None, max_length=255)
    kind: Literal["post", "story", "digest"] = "post"


class ContentRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    source: str = Field(default="studio", min_length=1, max_length=32)
    status: Literal["draft", "ready", "approved", "archived"] | None = None


class ContentSummaryResponse(BaseModel):
    id: int
    channel_id: int
    kind: str
    status: str
    title: str | None
    current_revision: int
    updated_at: datetime | None


class ContentDetailResponse(ContentSummaryResponse):
    document: dict[str, Any]


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]


class PreviewResponse(BaseModel):
    mode: str
    primary_text: str
    legacy_payload: dict[str, Any] | None
    publishable_via_legacy: bool
    reason: str | None = None


class TelegramPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    channel_id: int | None = Field(default=None, ge=1)
    replace_message_ids: list[int] = Field(default_factory=list, max_length=50)


class TelegramPreviewResponse(BaseModel):
    message_ids: list[int]


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime | None = None
    content_revision: int | None = Field(default=None, ge=1)
    timezone: str | None = Field(default=None, max_length=64)
    repeat_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    runtime_options: dict[str, Any] = Field(default_factory=dict)


class PublicationResponse(BaseModel):
    id: int
    content_item_id: int
    content_revision: int
    channel_id: int
    status: str
    schedule_entry_id: int | None
    legacy_post_task_id: int | None


class PlannerEntryResponse(BaseModel):
    schedule_id: int
    channel_id: int
    content_item_id: int
    content_revision: int
    content_title: str | None
    content_kind: str
    scheduled_at: datetime
    timezone: str | None
    schedule_status: str
    repeat_rule: dict[str, Any]
    publication_id: int | None
    publication_status: str | None
    telegram_message_ids: list[int] | None
    result_link: str | None
    last_error: str | None
    attempt_number: int | None
    attempt_status: str | None
    attempt_started_at: datetime | None
    attempt_finished_at: datetime | None
    legacy_post_task_id: int | None


class PlannerRescheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime
    timezone: str | None = Field(default=None, max_length=64)
