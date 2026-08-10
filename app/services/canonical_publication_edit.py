from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.publication_edit_persistence import (
    PublicationEditConflictError,
    PublicationEditPersistenceError,
    PublicationEditPersistenceService,
)
from app.services.publication_editor import (
    PublicationEditorView,
    load_owned_publication_editor_view,
)
from app.services.telegram_edit_outcome import (
    TelegramEditOutcome,
    TelegramEditOutcomeService,
    TelegramEditProvider,
)


class CanonicalPublicationEditSyncFailed(RuntimeError):
    """Telegram confirmed the edit, but canonical persistence did not commit."""

    def __init__(self, *, conflict: bool) -> None:
        super().__init__("canonical edit sync failed")
        self.conflict = bool(conflict)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationEditResult:
    publication_id: int
    previous_revision: int
    revision: int
    message_id: int
    telegram_message_ids: tuple[int, ...]
    attempted_message_ids: tuple[int, ...]


class CanonicalPublicationEditCoordinator:
    """Join truthful Telegram edit success to canonical revision persistence.

    The provider call is deliberately outside a database transaction. A canonical
    preflight catches already-stale editor state before the Telegram side effect, and
    the persistence service repeats ownership/lifecycle/revision checks afterwards.
    A race can still occur between those boundaries, so a post-provider persistence
    failure is reported explicitly instead of pretending the canonical state updated.
    """

    def __init__(
        self,
        *,
        provider: TelegramEditProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.provider = provider
        self.session_factory = session_factory

    async def _preflight(
        self,
        *,
        publication_id: int,
        tg_user_id: int,
        expected_revision: int,
    ) -> PublicationEditorView:
        async with self.session_factory() as session:
            view = await load_owned_publication_editor_view(
                session,
                publication_id=publication_id,
                tg_user_id=tg_user_id,
            )
        if view is None:
            raise PublicationEditPersistenceError("canonical edit is unavailable")
        if view.status != "published" or view.primary_message_id is None:
            raise PublicationEditConflictError("publication is not editable")
        if int(view.content_revision) != int(expected_revision):
            raise PublicationEditConflictError("canonical content revision changed")
        return view

    @staticmethod
    def _confirmed_message_ids(
        view: PublicationEditorView, outcome: TelegramEditOutcome
    ) -> list[int]:
        confirmed = int(outcome.message_id)
        ids = [int(value) for value in view.telegram_message_ids if int(value) != confirmed]
        ids.append(confirmed)
        return ids

    async def _persist(
        self,
        *,
        view: PublicationEditorView,
        tg_user_id: int,
        expected_revision: int,
        payload: Mapping[str, Any],
        outcome: TelegramEditOutcome,
    ) -> CanonicalPublicationEditResult:
        ids = self._confirmed_message_ids(view, outcome)
        try:
            async with self.session_factory() as session:
                persisted = await PublicationEditPersistenceService(session).persist_success(
                    publication_id=view.publication_id,
                    tg_user_id=tg_user_id,
                    expected_revision=expected_revision,
                    payload=payload,
                    telegram_message_ids=ids,
                )
        except PublicationEditConflictError:
            raise CanonicalPublicationEditSyncFailed(conflict=True) from None
        except PublicationEditPersistenceError:
            raise CanonicalPublicationEditSyncFailed(conflict=False) from None

        return CanonicalPublicationEditResult(
            publication_id=persisted.publication_id,
            previous_revision=persisted.previous_revision,
            revision=persisted.revision,
            message_id=int(outcome.message_id),
            telegram_message_ids=persisted.telegram_message_ids,
            attempted_message_ids=outcome.attempted_message_ids,
        )

    async def edit_text_and_persist(
        self,
        *,
        publication_id: int,
        tg_user_id: int,
        expected_revision: int,
        payload: Mapping[str, Any],
        text: str,
        parse_mode: str | None = "Markdown",
        disable_web_page_preview: bool = True,
    ) -> CanonicalPublicationEditResult:
        view = await self._preflight(
            publication_id=publication_id,
            tg_user_id=tg_user_id,
            expected_revision=expected_revision,
        )
        outcome = await TelegramEditOutcomeService(self.provider).edit_text(
            chat_id=view.tg_chat_id,
            primary_message_id=int(view.primary_message_id),
            candidate_message_ids=list(view.telegram_message_ids),
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        return await self._persist(
            view=view,
            tg_user_id=tg_user_id,
            expected_revision=expected_revision,
            payload=payload,
            outcome=outcome,
        )

    async def edit_media_and_persist(
        self,
        *,
        publication_id: int,
        tg_user_id: int,
        expected_revision: int,
        payload: Mapping[str, Any],
        media: Any,
    ) -> CanonicalPublicationEditResult:
        view = await self._preflight(
            publication_id=publication_id,
            tg_user_id=tg_user_id,
            expected_revision=expected_revision,
        )
        outcome = await TelegramEditOutcomeService(self.provider).edit_media(
            chat_id=view.tg_chat_id,
            primary_message_id=int(view.primary_message_id),
            candidate_message_ids=list(view.telegram_message_ids),
            media=media,
        )
        return await self._persist(
            view=view,
            tg_user_id=tg_user_id,
            expected_revision=expected_revision,
            payload=payload,
            outcome=outcome,
        )
