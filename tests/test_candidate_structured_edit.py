from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_rewrite import CandidateRewriteError, CandidateRewriteService, RewriteInput, RewriteOutput, candidate_rewrite_input_hash
from app.services.candidate_structured_edit_ai import structured_edit_input_variant
from app.services.candidate_structured_rewrite_ai import STRUCTURED_REWRITE_GENERATION_KIND


async def _seed(session, channel_id: int):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(channel_id=channel_id, kind="rss", value=f"https://example.com/{channel_id}.xml", reuse_policy="rewrite_with_attribution")
    content = "Source factual body about a confirmed launch."
    document = SourceDocument(
        connector_id=int(connector.id),
        channel_id=int(channel_id),
        external_id=f"edit-{channel_id}",
        title="Source",
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        source_url="https://example.com/source",
        meta={"reuse_policy": "rewrite_with_attribution"},
    )
    await repo.add_document(document)
    candidate = ContentCandidate(
        source_document_id=int(document.id),
        channel_id=int(channel_id),
        suggested_action="rewrite",
        meta={"reuse_policy": "rewrite_with_attribution"},
    )
    await repo.add_candidate(candidate)
    await session.commit()
    return document, candidate


async def _parent(session, document, candidate):
    payload = {"schema_version": 1, "mode": "rich", "blocks": [{"id": "p", "type": "paragraph", "content": "Current structured proposal"}], "telegram": {}, "metadata": {}}
    run = CandidateRewriteRun(candidate_id=int(candidate.id), provider="channel_ai_structured", model="model-v1", status="completed", input_hash=candidate_rewrite_input_hash(document, candidate, "rewrite_with_attribution"), input_chars=len(document.content), text="Current structured proposal", output={"generation_kind": STRUCTURED_REWRITE_GENERATION_KIND, "post_document": payload})
    session.add(run)
    await session.flush()
    candidate.meta = {**dict(candidate.meta or {}), "rewrite_run_id": int(run.id), "rewrite_provider": run.provider, "rewrite_model": run.model}
    await session.commit()
    return run, PostDocument.from_dict(payload)


class _Provider:
    name = "channel_ai_structured"
    model = "model-v1"

    def __init__(self, mutate=None):
        self.mutate = mutate
        self.calls = 0

    async def rewrite(self, _payload: RewriteInput) -> RewriteOutput:
        self.calls += 1
        if self.mutate:
            await self.mutate()
        payload = {"schema_version": 1, "mode": "rich", "blocks": [{"id": "p2", "type": "paragraph", "content": "Edited proposal with concise independent wording."}], "telegram": {}, "metadata": {}}
        return RewriteOutput(text="Edited proposal with concise independent wording.", metadata={"generation_kind": STRUCTURED_REWRITE_GENERATION_KIND, "post_document": payload})


def test_edit_variant_binds_parent_document_and_operation() -> None:
    document = PostDocument.from_dict({"schema_version": 1, "mode": "rich", "blocks": [{"id": "p", "type": "paragraph", "content": "Body"}], "telegram": {}, "metadata": {}})
    first = structured_edit_input_variant(parent_run_id=10, document=document, operation="shorten")
    assert first == structured_edit_input_variant(parent_run_id=10, document=document, operation="shorten")
    assert first != structured_edit_input_variant(parent_run_id=11, document=document, operation="shorten")
    assert first != structured_edit_input_variant(parent_run_id=10, document=document, operation="expand")


def test_expected_current_guard_rejects_stale_parent_before_provider_call() -> None:
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                document, candidate = await _seed(session, 831)
                parent, parent_document = await _parent(session, document, candidate)
                provider = _Provider()
                with pytest.raises(CandidateRewriteError, match="authority changed"):
                    await CandidateRewriteService(session).rewrite(channel_id=831, candidate_id=candidate.id, provider=provider, input_variant=structured_edit_input_variant(parent_run_id=int(parent.id), document=parent_document, operation="shorten"), expected_current_run_id=int(parent.id) + 1000)
                assert provider.calls == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_slow_edit_cannot_overwrite_newer_current_authority() -> None:
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                document, candidate = await _seed(session, 832)
                parent, parent_document = await _parent(session, document, candidate)
                replacement_id = None

                async def replace_current():
                    nonlocal replacement_id
                    current = await session.get(ContentCandidate, int(candidate.id))
                    replacement = CandidateRewriteRun(candidate_id=int(candidate.id), provider="channel_ai_structured", model="model-v1", status="completed", input_hash=candidate_rewrite_input_hash(document, candidate, "rewrite_with_attribution", "replacement"), input_chars=len(document.content), text="Newer proposal", output={"generation_kind": STRUCTURED_REWRITE_GENERATION_KIND, "rewrite_input_variant": "replacement", "post_document": {"schema_version": 1, "mode": "rich", "blocks": [{"id": "new", "type": "paragraph", "content": "Newer proposal"}], "telegram": {}, "metadata": {}}})
                    session.add(replacement)
                    await session.flush()
                    replacement_id = int(replacement.id)
                    assert current is not None
                    current.meta = {**dict(current.meta or {}), "rewrite_run_id": replacement_id, "rewrite_provider": replacement.provider, "rewrite_model": replacement.model}
                    await session.commit()

                with pytest.raises(CandidateRewriteError, match="authority changed during rewrite"):
                    await CandidateRewriteService(session).rewrite(channel_id=832, candidate_id=candidate.id, provider=_Provider(replace_current), input_variant=structured_edit_input_variant(parent_run_id=int(parent.id), document=parent_document, operation="shorten"), expected_current_run_id=int(parent.id))
                current = await session.get(ContentCandidate, int(candidate.id))
                assert current is not None and replacement_id is not None
                assert int((current.meta or {})["rewrite_run_id"]) == replacement_id
                rows = (await session.execute(select(CandidateRewriteRun).where(CandidateRewriteRun.id.not_in([int(parent.id), replacement_id])))).scalars().all()
                assert len(rows) == 1
                assert rows[0].status == "stale"
                assert rows[0].output["discard_reason"] == "rewrite_authority_changed"
        finally:
            await engine.dispose()
    asyncio.run(run())
