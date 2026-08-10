from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.content.models import ContentItem
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_edit_autodelete import (
    PublicationEditAutodeleteSyncError,
    validate_publication_edit_autodelete_execution,
)
from app.services.publication_edit_persistence import (
    PublicationEditConflictError,
    PublicationEditPersistenceError,
    PublicationEditPersistenceService,
    edited_publication_runtime_options,
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

    def __init__(self, *, conflict: bool, error_type: str) -> None:
        super().__init__("canonical edit sync failed")
        self.conflict = bool(conflict)
        self.error_type = str(error_type or "Exception")[:120]


@dataclass(frozen=True, slots=True)
class CanonicalPublicationEditResult:
    publication_id: int
    previous_revision: int
    revision: int
    tg_chat_id: int
    message_id: int
    telegram_message_ids: tuple[int, ...]
    attempted_message_ids: tuple[int, ...]


class CanonicalPublicationEditCoordinator:
    """Join truthful Telegram edit success to canonical revision persistence.

    The provider call is deliberately outside a database transaction. A canonical
    preflight catches already-stale editor/transport state and unsupported runtime
    intent before the Telegram side effect, and persistence repeats those checks after
    provider success. A race can still occur between the boundaries, so a post-provider
    persistence failure is reported explicitly instead of pretending canonical state
    updated.
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
        payload: Mapping[str, Any],
    ) -> PublicationEditorView:
        async with self.session_factory() as session:
            view = await load_owned_publication_editor_view(
                session,
                publication_id=publication_id,
                tg_user_id=tg_user_id,
            )
            if view is None:
                raise PublicationEditPersistenceError("canonical edit is unavailable")

            lifecycle = (
                await session.execute(
                    select(
                        Publication.status,
                        Publication.content_revision,
                        Publication.legacy_post_task_id,
                        Publication.channel_id,
                        ScheduleEntry.status,
                        ScheduleEntry.content_revision,
                        ContentItem.current_revision,
                        ContentItem.kind,
                    )
                    .join(
                        ScheduleEntry,
                        ScheduleEntry.id == Publication.schedule_entry_id,
                    )
                    .join(ContentItem, ContentItem.id == Publication.content_item_id)
                    .where(
                        Publication.id == int(view.publication_id),
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                    )
                )
            ).one_or_none()
            if lifecycle is None:
                raise PublicationEditConflictError(
                    "canonical publication linkage changed"
                )
            (
                publication_status,
                publication_revision,
                legacy_post_task_id,
                publication_channel_id,
                schedule_status,
                schedule_revision,
                current_revision,
                content_kind,
            ) = lifecycle

            if legacy_post_task_id is not None:
                transport = (
                    await session.execute(
                        select(PostTask.status, PostTask.channel_id).where(
                            PostTask.id == int(legacy_post_task_id)
                        )
                    )
                ).one_or_none()
                if transport is None:
                    raise PublicationEditConflictError(
                        "linked legacy transport is missing"
                    )
                transport_status, transport_channel_id = transport
                if (
                    str(transport_status or "") != "done"
                    or int(transport_channel_id) != int(publication_channel_id)
                ):
                    raise PublicationEditConflictError(
                        "linked legacy transport is not consistently published"
                    )

        safe_expected_revision = int(expected_revision)
        if view.primary_message_id is None:
            raise PublicationEditConflictError("publication is not editable")
        if (
            str(publication_status or "") != "published"
            or str(schedule_status or "") != "completed"
        ):
            raise PublicationEditConflictError("publication lifecycle is not editable")
        if str(content_kind or "") != "post":
            raise PublicationEditConflictError("content item is not editable")
        if not (
            int(view.content_revision) == safe_expected_revision
            and int(publication_revision or 0) == safe_expected_revision
            and int(schedule_revision or 0) == safe_expected_revision
            and int(current_revision or 0) == safe_expected_revision
        ):
            raise PublicationEditConflictError("canonical content revision changed")

        runtime_options = edited_publication_runtime_options(
            view.publication_meta.get("runtime_options"),
            payload,
        )
        try:
            validate_publication_edit_autodelete_execution(
                legacy_post_task_id=(
                    int(legacy_post_task_id)
                    if legacy_post_task_id is not None
                    else None
                ),
                runtime_options=runtime_options,
            )
        except PublicationEditAutodeleteSyncError as exc:
            raise PublicationEditPersistenceError(str(exc)) from None
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
        except PublicationEditConflictError as exc:
            raise CanonicalPublicationEditSyncFailed(
                conflict=True,
                error_type=type(exc).__name__,
            ) from None
        except PublicationEditPersistenceError as exc:
            raise CanonicalPublicationEditSyncFailed(
                conflict=False,
                error_type=type(exc).__name__,
            ) from None
        except Exception as exc:
            raise CanonicalPublicationEditSyncFailed(
                conflict=False,
                error_type=type(exc).__name__,
            ) from None

        return CanonicalPublicationEditResult(
            publication_id=persisted.publication_id,
            previous_revision=persisted.previous_revision,
            revision=persisted.revision,
            tg_chat_id=int(view.tg_chat_id),
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
            payload=payload,
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
            payload=payload,
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
