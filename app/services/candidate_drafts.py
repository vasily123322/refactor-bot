from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.sources.models import SourceDocument
from app.repositories.sources_v2 import SourcesRepo


class CandidateDraftError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateDraftResult:
    item: ContentItem
    document: PostDocument
    reused_existing: bool


_MAX_DRAFT_TEXT = 3900
_MAX_QUOTE_CHARS = 700


def _source_label(document: SourceDocument) -> str:
    if document.source_url:
        return f"Источник: {document.source_url}"
    if document.title:
        return f"Источник: {document.title}"
    return "Источник сохранён в Sources Inbox"


def _clip(value: str, limit: int) -> tuple[str, bool]:
    text = str(value).strip()
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)].rstrip() + "…", True


def _draft_text(*, policy: str, document: SourceDocument, summary: str | None) -> tuple[str, bool]:
    title = (document.title or "Материал из источника").strip()
    source = _source_label(document)
    body = str(document.content or "").strip()

    if policy == "mirror_authorized":
        text = f"{body}\n\n{source}" if body else source
        return _clip(text, _MAX_DRAFT_TEXT)

    if policy == "quote_with_attribution":
        quote, quote_truncated = _clip(body, _MAX_QUOTE_CHARS)
        parts = [title]
        if quote:
            parts.append(f"«{quote}»")
        parts.append(source)
        text, text_truncated = _clip("\n\n".join(parts), _MAX_DRAFT_TEXT)
        return text, quote_truncated or text_truncated

    if policy == "summarize" and summary:
        text, truncated = _clip(f"{summary.strip()}\n\n{source}", _MAX_DRAFT_TEXT)
        return text, truncated

    task = {
        "summarize": "Задача: подготовить краткое изложение своими словами.",
        "rewrite_with_attribution": "Задача: подготовить самостоятельный текст по материалу с attribution.",
        "reference_only": "Задача: использовать источник только как reference для собственного текста.",
    }.get(policy, "Задача: проверить материал и подготовить самостоятельный текст.")
    text = f"{title}\n\n{source}\n\n{task}\n\nЗаметки:\n"
    return _clip(text, _MAX_DRAFT_TEXT)


class CandidateDraftService:
    """Accept an Inbox candidate into Content while enforcing source reuse policy."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.sources = SourcesRepo(session)

    async def create(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        created_by_tg_user_id: int | None = None,
    ) -> CandidateDraftResult:
        candidate = await self.sources.get_candidate_for_channel(candidate_id, channel_id)
        if candidate is None:
            raise CandidateDraftError("candidate not found")

        if candidate.content_item_id is not None:
            item = await self.session.get(ContentItem, int(candidate.content_item_id))
            if item is None or int(item.channel_id) != int(channel_id):
                raise CandidateDraftError("candidate draft link is inconsistent")
            revision = await self.session.get(
                ContentRevision,
                {
                    "content_item_id": int(item.id),
                    "revision": int(item.current_revision),
                },
            )
            if revision is None:
                # ContentRevision has a surrogate primary key, so fetch through the
                # stable document representation stored in candidate metadata below.
                from sqlalchemy import select

                revision = (
                    await self.session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == int(item.id),
                            ContentRevision.revision == int(item.current_revision),
                        )
                    )
                ).scalar_one_or_none()
            if revision is None:
                raise CandidateDraftError("candidate draft has no current revision")
            return CandidateDraftResult(
                item=item,
                document=PostDocument.from_dict(revision.document),
                reused_existing=True,
            )

        source_document = await self.session.get(SourceDocument, int(candidate.source_document_id))
        if source_document is None or int(source_document.channel_id) != int(channel_id):
            raise CandidateDraftError("candidate source document not found")
        connector = await self.sources.get_connector_for_channel(
            int(source_document.connector_id), int(channel_id)
        )
        if connector is None:
            raise CandidateDraftError("candidate source connector not found")

        policy = str(connector.reuse_policy or "reference_only")
        text, truncated = _draft_text(
            policy=policy,
            document=source_document,
            summary=candidate.summary,
        )
        document = PostDocument(
            blocks=[{"id": "b1", "type": "text", "text": text, "entities": []}],
            metadata={
                "source_candidate_id": int(candidate.id),
                "source_document_id": int(source_document.id),
                "source_connector_id": int(connector.id),
                "source_url": source_document.source_url,
                "reuse_policy": policy,
                "suggested_action": candidate.suggested_action,
                "source_body_truncated": bool(truncated),
            },
        )
        document.validate()

        item = ContentItem(
            channel_id=int(channel_id),
            kind="post",
            status="draft",
            title=(candidate.topic or source_document.title or f"Inbox #{candidate.id}")[:255],
            current_revision=0,
            meta={
                "created_from": "source_candidate",
                "source_candidate_id": int(candidate.id),
                "reuse_policy": policy,
            },
        )
        self.session.add(item)
        try:
            await self.session.flush()
            revision = ContentRevision(
                content_item_id=int(item.id),
                revision=1,
                document=document.to_dict(),
                source="source_candidate",
                created_by_tg_user_id=created_by_tg_user_id,
                meta={"source_document_id": int(source_document.id)},
            )
            self.session.add(revision)
            item.current_revision = 1
            candidate.content_item_id = int(item.id)
            candidate.status = "accepted"
            await self.session.commit()
            await self.session.refresh(item)
            return CandidateDraftResult(
                item=item,
                document=document,
                reused_existing=False,
            )
        except Exception:
            await self.session.rollback()
            raise
