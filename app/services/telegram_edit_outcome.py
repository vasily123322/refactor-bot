from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from aiogram.exceptions import TelegramBadRequest

from app.services.telegram_results import normalize_telegram_message_ids


class TelegramEditProvider(Protocol):
    async def edit_message_text(self, **kwargs): ...

    async def edit_message_media(self, **kwargs): ...


class TelegramEditFailed(RuntimeError):
    """Safe provider failure that never embeds raw Telegram exception text."""

    def __init__(self, *, provider_error_type: str) -> None:
        super().__init__("telegram edit failed")
        self.provider_error_type = str(provider_error_type or "Exception")[:120]


@dataclass(frozen=True, slots=True)
class TelegramEditOutcome:
    message_id: int
    attempted_message_ids: tuple[int, ...]


def _candidate_message_ids(
    primary_message_id: int,
    candidate_message_ids: list[int] | tuple[int, ...] | None,
) -> list[int]:
    normalized_primary = normalize_telegram_message_ids([primary_message_id])
    normalized_candidates = normalize_telegram_message_ids(candidate_message_ids)
    ordered = normalized_primary + list(reversed(normalized_candidates))
    unique: list[int] = []
    seen: set[int] = set()
    for message_id in ordered:
        value = int(message_id)
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


class TelegramEditOutcomeService:
    """Perform an edit and return success only after Telegram confirms one attempt.

    Fallback to alternate message ids is only used for TelegramBadRequest, which is
    the class of errors expected when a legacy primary id points at the wrong message
    inside an old multi-message result. Unexpected provider/network failures are not
    retried against other messages and are converted into a safe generic error.
    """

    def __init__(self, provider: TelegramEditProvider) -> None:
        self.provider = provider

    async def edit_text(
        self,
        *,
        chat_id: int,
        primary_message_id: int,
        candidate_message_ids: list[int] | tuple[int, ...] | None,
        text: str,
        parse_mode: str | None = "Markdown",
        disable_web_page_preview: bool = True,
    ) -> TelegramEditOutcome:
        candidates = _candidate_message_ids(primary_message_id, candidate_message_ids)
        if not candidates:
            raise TelegramEditFailed(provider_error_type="InvalidMessageId")

        attempted: list[int] = []
        last_bad_request_type = "TelegramBadRequest"
        for message_id in candidates:
            attempted.append(message_id)
            try:
                await self.provider.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                return TelegramEditOutcome(
                    message_id=message_id,
                    attempted_message_ids=tuple(attempted),
                )
            except TelegramBadRequest as exc:
                last_bad_request_type = type(exc).__name__
                continue
            except Exception as exc:
                raise TelegramEditFailed(
                    provider_error_type=type(exc).__name__
                ) from None

        raise TelegramEditFailed(provider_error_type=last_bad_request_type) from None

    async def edit_media(
        self,
        *,
        chat_id: int,
        primary_message_id: int,
        candidate_message_ids: list[int] | tuple[int, ...] | None,
        media: Any,
    ) -> TelegramEditOutcome:
        candidates = _candidate_message_ids(primary_message_id, candidate_message_ids)
        if not candidates:
            raise TelegramEditFailed(provider_error_type="InvalidMessageId")

        attempted: list[int] = []
        last_bad_request_type = "TelegramBadRequest"
        for message_id in candidates:
            attempted.append(message_id)
            try:
                await self.provider.edit_message_media(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    media=media,
                )
                return TelegramEditOutcome(
                    message_id=message_id,
                    attempted_message_ids=tuple(attempted),
                )
            except TelegramBadRequest as exc:
                last_bad_request_type = type(exc).__name__
                continue
            except Exception as exc:
                raise TelegramEditFailed(
                    provider_error_type=type(exc).__name__
                ) from None

        raise TelegramEditFailed(provider_error_type=last_bad_request_type) from None
