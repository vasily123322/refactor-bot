from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_rewrite import current_candidate_rewrite_run


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


def _body_with_source(body: str, source: str) -> tuple[str, bool]:
    suffix = f"\n\n{source}"
    body_limit = max(0, _MAX_DRAFT_TEXT - len(suffix))
    clipped, truncated = _clip(body, body_limit)
    return (f"{clipped}{suffix}" if clipped else source), truncated


def _draft_text(
    *,
    policy: str,
    document: SourceDocument,
    summary: str | None,
    generated_rewrite: str | None = None,
) -> tuple[str, bool]:
    title = (document.title or "Материал из источника").strip()
    source = _source_label(document)
    body = str(document.content or "").strip()

    if policy == "mirror_authorized":
        return _body_with_source(body, source)

    if policy == "quote_with_attribution":
        quote, quote_truncated = _clip(body, _MAX_QUOTE_CHARS)
        parts = [title]
        if quote:
            parts.append(f"«{quote}»")
        prefix = "\n\n".join(parts)
        text, text_truncated = _body_with_source(prefix, source)
        return text, quote_truncated or text_truncated

    if policy == "summarize" and summary:
        return _body_with_source(summary.strip(), source)

    if policy == "rewrite_with_attribution" and generated_rewrite:
        return _body_with_source(generated_rewrite, source)

    task = {
        "summarize": "Задача: подготовить краткое изложение своими словами.",
        "rewrite_with_attribution": (
            "Задача: подготовить самостоятельный текст по материалу с attribution."
        ),
        "reference_only": (
            "Задача: использовать источник только как reference для собственного текста."
        ),
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
        candidate = (
            await self.session.execute(
                select(ContentCandidate)
                .where(
                    ContentCandidate.id == int(candidate_id),
                    ContentCandidate.channel_id == int(channel_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise CandidateDraftError("candidate not found")

        if candidate.content_item_id is not None:
            item = await self.session.get(ContentItem, int(candidate.content_item_id))
            if item is None or int(item.channel_id) != int(channel_id):
                raise CandidateDraftError("candidate draft link is inconsistent")
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

        source_document = await self.session.get(
            SourceDocument, int(candidate.source_document_id)
        )
        if source_document is None or int(source_document.channel_id) != int(channel_id):
            raise CandidateDraftError("candidate source document not found")
        connector = await self.sources.get_connector_for_channel(
            int(source_document.connector_id), int(channel_id)
        )
        if connector is None:
            raise CandidateDraftError("candidate source connector not found")

        policy = str(connector.reuse_policy or "reference_only")
        rewrite_run = None
        if policy == "rewrite_with_attribution":
            rewrite_run = await current_candidate_rewrite_run(
                self.session,
                candidate=candidate,
                document=source_document,
                reuse_policy=policy,
            )
        text, truncated = _draft_text(
            policy=policy,
            document=source_document,
            summary=candidate.summary,
            generated_rewrite=(rewrite_run.text if rewrite_run is not None else None),
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
                "rewrite_run_id": (
                    int(rewrite_run.id) if rewrite_run is not None else None
                ),
                "rewrite_provider": (
                    str(rewrite_run.provider) if rewrite_run is not None else None
                ),
                "rewrite_model": rewrite_run.model if rewrite_run is not None else None,
            },
        )
        document.validate()

        item = ContentItem(
            channel_id=int(channel_id),
            kind="post",
            status="draft",
            title=(
                candidate.topic or source_document.title or f"Inbox #{candidate.id}"
            )[:255],
            current_revision=0,
            meta={
                "created_from": "source_candidate",
                "source_candidate_id": int(candidate.id),
                "reuse_policy": policy,
                "rewrite_run_id": (
                    int(rewrite_run.id) if rewrite_run is not None else None
                ),
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
                meta={
                    "source_document_id": int(source_document.id),
                    "rewrite_run_id": (
                        int(rewrite_run.id) if rewrite_run is not None else None
                    ),
                },
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
