from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentItem
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_current_structured_rewrite import CandidateCurrentStructuredRewriteService
from app.services.candidate_prompt_structured_rewrite_ai import (
    prompt_structured_rewrite_input_variant,
)
from app.services.candidate_rewrite import (
    CandidateRewriteService,
    RewriteInput,
    RewriteOutput,
    candidate_rewrite_input_hash,
)
from app.services.candidate_structured_rewrite_ai import STRUCTURED_REWRITE_GENERATION_KIND


class _StructuredProvider:
    name = "channel_ai_structured"
    model = "model-v1"

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def rewrite(self, _payload: RewriteInput) -> RewriteOutput:
        self.calls += 1
        document = {
            "schema_version": 1,
            "mode": "rich",
            "blocks": [{"id": "p", "type": "paragraph", "content": self.text}],
            "telegram": {},
            "metadata": {},
        }
        return RewriteOutput(
            text=self.text,
            metadata={
                "generation_kind": STRUCTURED_REWRITE_GENERATION_KIND,
                "source_is_untrusted": True,
                "post_document": document,
            },
        )


async def _seed(session, *, channel_id: int = 821):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value=f"https://example.com/{channel_id}.xml",
        reuse_policy="rewrite_with_attribution",
    )
    document, _ = await repo.upsert_document(
        connector=connector,
        external_id="prompt-entry",
        title="Source title",
        content="Source facts about a product launch and its confirmed date.",
        source_url="https://example.com/source",
        metadata={"reuse_policy": "rewrite_with_attribution"},
    )
    candidate = await repo.ensure_candidate(
        source_document_id=document.id,
        channel_id=channel_id,
        suggested_action="rewrite",
        metadata={"reuse_policy": "rewrite_with_attribution"},
    )
    return document, candidate


def test_default_rewrite_hash_is_backward_compatible_and_prompt_variant_is_distinct() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                document, candidate = await _seed(session)
                material = "\0".join(
                    [
                        str(document.content_hash or ""),
                        str(document.title or ""),
                        str(document.source_url or ""),
                        str(candidate.suggested_action or ""),
                        "rewrite_with_attribution",
                    ]
                )
                legacy = hashlib.sha256(material.encode("utf-8")).hexdigest()
                assert candidate_rewrite_input_hash(
                    document,
                    candidate,
                    "rewrite_with_attribution",
                ) == legacy
                first = prompt_structured_rewrite_input_variant("Сделай короче")
                assert first == prompt_structured_rewrite_input_variant("  Сделай короче  ")
                assert first != prompt_structured_rewrite_input_variant("Добавь подзаголовки")
                assert candidate_rewrite_input_hash(
                    document,
                    candidate,
                    "rewrite_with_attribution",
                    first,
                ) != legacy
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_same_prompt_reuses_run_different_prompt_becomes_current_and_recovers() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, candidate = await _seed(session, channel_id=822)
                service = CandidateRewriteService(session)
                first_provider = _StructuredProvider("First prompted proposal with concise wording.")
                first_variant = prompt_structured_rewrite_input_variant("Сделай короче")
                first = await service.rewrite(
                    channel_id=822,
                    candidate_id=candidate.id,
                    provider=first_provider,
                    input_variant=first_variant,
                )
                assert first.reused_existing is False
                assert first_provider.calls == 1

                same_provider = _StructuredProvider("This output must never be generated.")
                same = await service.rewrite(
                    channel_id=822,
                    candidate_id=candidate.id,
                    provider=same_provider,
                    input_variant=first_variant,
                )
                assert same.reused_existing is True
                assert int(same.run.id) == int(first.run.id)
                assert same_provider.calls == 0

                second_provider = _StructuredProvider("Second proposal with two clear headings in spirit.")
                second_variant = prompt_structured_rewrite_input_variant("Добавь два подзаголовка")
                second = await service.rewrite(
                    channel_id=822,
                    candidate_id=candidate.id,
                    provider=second_provider,
                    input_variant=second_variant,
                )
                assert second.reused_existing is False
                assert int(second.run.id) != int(first.run.id)
                assert int((second.candidate.meta or {})["rewrite_run_id"]) == int(second.run.id)
                old = await session.get(CandidateRewriteRun, int(first.run.id))
                assert old is not None and old.status == "completed"

            async with Session() as refreshed:
                current = await CandidateCurrentStructuredRewriteService(refreshed).current(
                    channel_id=822,
                    candidate_id=1,
                )
                assert current is not None
                assert int(current.run.id) == int(second.run.id)
                assert current.document.primary_text().startswith("Second proposal")
                assert (
                    await refreshed.execute(
                        select(ContentItem).where(ContentItem.channel_id == 822)
                    )
                ).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())
