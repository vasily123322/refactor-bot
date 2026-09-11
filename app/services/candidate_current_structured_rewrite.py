from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.services.candidate_drafts import CandidateDraftResult, CandidateDraftService
from app.services.candidate_rewrite import (
    CandidateRewriteError,
    current_candidate_rewrite_run,
)
from app.services.candidate_structured_rewrite_ai import (
    ChannelAIStructuredRewriteProvider,
    structured_document_from_run_output,
)


class CandidateCurrentStructuredRewriteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CurrentCandidateStructuredRewrite:
    candidate: ContentCandidate
    document_source: SourceDocument
    connector: SourceConnector
    run: CandidateRewriteRun
    document: PostDocument


def structured_document_from_current_rewrite_run(
    *,
    candidate: ContentCandidate,
    run: CandidateRewriteRun,
) -> PostDocument | None:
    """Resolve structured authority only from the candidate's persisted current pointer."""

    metadata = dict(candidate.meta or {})
    try:
        current_run_id = int(metadata.get("rewrite_run_id"))
    except (TypeError, ValueError):
        return None
    if current_run_id != int(run.id):
        return None
    if str(run.provider) != str(ChannelAIStructuredRewriteProvider.name):
        return None

    pointer_provider = metadata.get("rewrite_provider")
    if pointer_provider is not None and str(pointer_provider) != str(run.provider):
        return None
    pointer_model = metadata.get("rewrite_model")
    if pointer_model is not None and str(pointer_model or "") != str(run.model or ""):
        return None

    return structured_document_from_run_output(run.output)


class CandidateCurrentStructuredRewriteService:
    """Read/apply the persisted current structured proposal without inventing new authority."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _load(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        for_update: bool = False,
    ) -> tuple[ContentCandidate, SourceDocument, SourceConnector] | None:
        stmt = (
            select(ContentCandidate, SourceDocument, SourceConnector)
            .join(SourceDocument, SourceDocument.id == ContentCandidate.source_document_id)
            .join(SourceConnector, SourceConnector.id == SourceDocument.connector_id)
            .where(
                ContentCandidate.id == int(candidate_id),
                ContentCandidate.channel_id == int(channel_id),
                SourceDocument.channel_id == int(channel_id),
                SourceConnector.channel_id == int(channel_id),
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return row[0], row[1], row[2]

    async def _resolve(
        self,
        *,
        candidate: ContentCandidate,
        document: SourceDocument,
        connector: SourceConnector,
    ) -> CurrentCandidateStructuredRewrite | None:
        if str(candidate.status or "new") != "new":
            return None
        policy = str(connector.reuse_policy or "reference_only")
        if policy != "rewrite_with_attribution":
            return None

        run = await current_candidate_rewrite_run(
            self.session,
            candidate=candidate,
            document=document,
            reuse_policy=policy,
        )
        if run is None:
            return None
        try:
            proposal = structured_document_from_current_rewrite_run(
                candidate=candidate,
                run=run,
            )
        except CandidateRewriteError:
            return None
        if proposal is None:
            return None
        return CurrentCandidateStructuredRewrite(
            candidate=candidate,
            document_source=document,
            connector=connector,
            run=run,
            document=proposal,
        )

    async def current(
        self,
        *,
        channel_id: int,
        candidate_id: int,
    ) -> CurrentCandidateStructuredRewrite | None:
        loaded = await self._load(channel_id=channel_id, candidate_id=candidate_id)
        if loaded is None:
            return None
        return await self._resolve(
            candidate=loaded[0],
            document=loaded[1],
            connector=loaded[2],
        )

    async def apply(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        expected_run_id: int,
        created_by_tg_user_id: int | None = None,
    ) -> CandidateDraftResult:
        loaded = await self._load(
            channel_id=channel_id,
            candidate_id=candidate_id,
            for_update=True,
        )
        if loaded is None:
            raise CandidateCurrentStructuredRewriteError("candidate not found")
        current = await self._resolve(
            candidate=loaded[0],
            document=loaded[1],
            connector=loaded[2],
        )
        if current is None or int(current.run.id) != int(expected_run_id):
            raise CandidateCurrentStructuredRewriteError(
                "candidate structured rewrite is no longer current"
            )

        # CandidateDraftService runs in the same transaction. The candidate row lock
        # keeps the persisted pointer stable while the normal apply path reparses and
        # validates the run before creating canonical Content.
        return await CandidateDraftService(self.session).create(
            channel_id=channel_id,
            candidate_id=candidate_id,
            created_by_tg_user_id=created_by_tg_user_id,
        )
