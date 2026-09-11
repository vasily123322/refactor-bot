from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.services.ai_run_leases import ai_run_lease_expired, abandon_expired_ai_run


_MAX_REWRITE_INPUT_CHARS = 12_000
_MAX_REWRITE_OUTPUT_CHARS = 3_200
_MAX_REWRITE_INPUT_VARIANT_CHARS = 191
_REWRITE_INPUT_VARIANT_OUTPUT_KEY = "rewrite_input_variant"


class CandidateRewriteError(RuntimeError):
    pass


class CandidateRewriteBusy(CandidateRewriteError):
    pass


@dataclass(frozen=True, slots=True)
class RewriteInput:
    candidate_id: int
    source_document_id: int
    title: str | None
    source_url: str | None
    text: str
    reuse_policy: str


@dataclass(frozen=True, slots=True)
class RewriteOutput:
    text: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CandidateRewriteResult:
    candidate: ContentCandidate
    run: CandidateRewriteRun
    reused_existing: bool


class CandidateRewriteProvider(Protocol):
    name: str
    model: str

    async def rewrite(self, payload: RewriteInput) -> RewriteOutput: ...


def _bounded(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _comparison_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _candidate_current_rewrite_run_id(candidate: ContentCandidate) -> int | None:
    value = (candidate.meta or {}).get("rewrite_run_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _validate_independent_rewrite(
    *,
    source_text: str,
    generated: str,
    source_url: str | None,
) -> None:
    """Fail closed on obvious copy/mirror output before it becomes publishable."""

    source = _comparison_text(source_text)
    rewrite = _comparison_text(generated)
    if not source or not rewrite:
        return
    if source == rewrite:
        raise CandidateRewriteError("rewrite is too similar to source")

    if source_url and source_url.casefold() in generated.casefold():
        raise CandidateRewriteError("rewrite must not include source attribution")

    if len(source) >= 120 and source in rewrite:
        raise CandidateRewriteError("rewrite contains verbatim source text")

    if min(len(source), len(rewrite)) >= 160:
        matcher = SequenceMatcher(a=source, b=rewrite, autojunk=False)
        if matcher.ratio() >= 0.82:
            raise CandidateRewriteError("rewrite is too similar to source")
        longest = matcher.find_longest_match(0, len(source), 0, len(rewrite)).size
        if longest >= 220:
            raise CandidateRewriteError("rewrite contains a long verbatim passage")


def candidate_rewrite_input_hash(
    document: SourceDocument,
    candidate: ContentCandidate,
    reuse_policy: str,
    input_variant: str | None = None,
) -> str:
    parts = [
        str(document.content_hash or ""),
        str(document.title or ""),
        str(document.source_url or ""),
        str(candidate.suggested_action or ""),
        str(reuse_policy),
    ]
    if input_variant:
        parts.append(str(input_variant))
    material = "\0".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def rewrite_run_input_variant(run: CandidateRewriteRun) -> str | None:
    output = run.output if isinstance(run.output, dict) else {}
    value = output.get(_REWRITE_INPUT_VARIANT_OUTPUT_KEY)
    if value is None:
        return None
    normalized = _bounded(str(value), _MAX_REWRITE_INPUT_VARIANT_CHARS)
    return normalized or None


async def current_candidate_rewrite_run(
    session: AsyncSession,
    *,
    candidate: ContentCandidate,
    document: SourceDocument,
    reuse_policy: str,
) -> CandidateRewriteRun | None:
    run_id = _candidate_current_rewrite_run_id(candidate)
    if run_id is None:
        return None
    run = await session.get(CandidateRewriteRun, run_id)
    if run is None:
        return None
    if int(run.candidate_id) != int(candidate.id) or str(run.status) != "completed":
        return None
    if run.input_hash != candidate_rewrite_input_hash(
        document,
        candidate,
        reuse_policy,
        rewrite_run_input_variant(run),
    ):
        return None
    if not str(run.text or "").strip():
        return None
    return run


class CandidateRewriteService:
    """Generate an independent rewrite only for explicit rewrite-with-attribution policy."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _load(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        for_update: bool = False,
        refresh: bool = False,
    ) -> tuple[ContentCandidate, SourceDocument, SourceConnector]:
        stmt = (
            select(ContentCandidate, SourceDocument, SourceConnector)
            .join(
                SourceDocument,
                SourceDocument.id == ContentCandidate.source_document_id,
            )
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
        if refresh:
            stmt = stmt.execution_options(populate_existing=True)
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            raise CandidateRewriteError("candidate not found")
        return row[0], row[1], row[2]

    async def _refresh_run(self, run_id: int) -> CandidateRewriteRun:
        run = await self.session.get(CandidateRewriteRun, int(run_id))
        if run is None:
            raise CandidateRewriteError("rewrite run disappeared")
        await self.session.refresh(run)
        return run

    async def _release_read_lock(self) -> None:
        await self.session.commit()

    async def rewrite(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        provider: CandidateRewriteProvider,
        input_variant: str | None = None,
        expected_current_run_id: int | None = None,
    ) -> CandidateRewriteResult:
        provider_name = _bounded(str(provider.name), 64)
        model_name = _bounded(str(provider.model), 191)
        variant = _bounded(str(input_variant or ""), _MAX_REWRITE_INPUT_VARIANT_CHARS) or None
        expected_run_id = (
            int(expected_current_run_id)
            if expected_current_run_id is not None
            else None
        )
        if not provider_name or not model_name:
            raise CandidateRewriteError("rewrite provider identity is required")

        candidate, document, connector = await self._load(
            channel_id=channel_id,
            candidate_id=candidate_id,
            for_update=True,
            refresh=True,
        )
        if str(candidate.status or "new") != "new":
            raise CandidateRewriteError("candidate is no longer active")
        policy = str(connector.reuse_policy or "reference_only")
        if policy != "rewrite_with_attribution":
            raise CandidateRewriteError("candidate policy does not allow AI rewrite")
        if (
            expected_run_id is not None
            and _candidate_current_rewrite_run_id(candidate) != expected_run_id
        ):
            raise CandidateRewriteError("candidate rewrite authority changed")

        input_hash = candidate_rewrite_input_hash(document, candidate, policy, variant)
        completed = (
            await self.session.execute(
                select(CandidateRewriteRun)
                .where(
                    CandidateRewriteRun.candidate_id == int(candidate.id),
                    CandidateRewriteRun.provider == provider_name,
                    CandidateRewriteRun.model == model_name,
                    CandidateRewriteRun.input_hash == input_hash,
                    CandidateRewriteRun.status == "completed",
                )
                .order_by(CandidateRewriteRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if completed is not None:
            candidate.meta = {
                **dict(candidate.meta or {}),
                "rewrite_run_id": int(completed.id),
                "rewrite_provider": provider_name,
                "rewrite_model": model_name,
            }
            await self.session.commit()
            await self.session.refresh(candidate)
            return CandidateRewriteResult(candidate, completed, True)

        running_rows = list(
            (
                await self.session.execute(
                    select(CandidateRewriteRun)
                    .where(
                        CandidateRewriteRun.candidate_id == int(candidate.id),
                        CandidateRewriteRun.provider == provider_name,
                        CandidateRewriteRun.model == model_name,
                        CandidateRewriteRun.input_hash == input_hash,
                        CandidateRewriteRun.status == "running",
                    )
                    .order_by(CandidateRewriteRun.id.desc())
                )
            ).scalars().all()
        )
        if any(not ai_run_lease_expired(row.started_at) for row in running_rows):
            await self._release_read_lock()
            raise CandidateRewriteBusy("candidate rewrite is already running")
        for stale_run in running_rows:
            abandon_expired_ai_run(stale_run)

        text = _bounded(str(document.content or ""), _MAX_REWRITE_INPUT_CHARS)
        payload = RewriteInput(
            candidate_id=int(candidate.id),
            source_document_id=int(document.id),
            title=document.title,
            source_url=document.source_url,
            text=text,
            reuse_policy=policy,
        )
        run = CandidateRewriteRun(
            candidate_id=int(candidate.id),
            provider=provider_name,
            model=model_name,
            status="running",
            input_hash=input_hash,
            input_chars=len(text),
            output=(
                {_REWRITE_INPUT_VARIANT_OUTPUT_KEY: variant}
                if variant is not None
                else {}
            ),
        )
        self.session.add(run)
        try:
            await self.session.commit()
            await self.session.refresh(run)
        except Exception:
            await self.session.rollback()
            raise

        try:
            output = await provider.rewrite(payload)
            generated = _bounded(output.text, _MAX_REWRITE_OUTPUT_CHARS)
            if not generated:
                raise CandidateRewriteError("rewrite provider returned empty text")
            _validate_independent_rewrite(
                source_text=text,
                generated=generated,
                source_url=document.source_url,
            )
            output_meta = dict(output.metadata or {})
            if variant is not None:
                output_meta[_REWRITE_INPUT_VARIANT_OUTPUT_KEY] = variant
        except Exception as exc:
            await self._load(
                channel_id=channel_id,
                candidate_id=candidate_id,
                for_update=True,
                refresh=True,
            )
            persisted = await self._refresh_run(run.id)
            if str(persisted.status) != "running":
                await self._release_read_lock()
                raise CandidateRewriteError("rewrite run is no longer active") from exc
            persisted.status = "failed"
            persisted.error = type(exc).__name__
            persisted.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
            if isinstance(exc, CandidateRewriteError):
                raise
            raise CandidateRewriteError("candidate rewrite failed") from exc

        candidate, document, connector = await self._load(
            channel_id=channel_id,
            candidate_id=candidate_id,
            for_update=True,
            refresh=True,
        )
        persisted = await self._refresh_run(run.id)
        if str(persisted.status) != "running":
            await self._release_read_lock()
            raise CandidateRewriteError("rewrite run is no longer active")
        current_policy = str(connector.reuse_policy or "reference_only")

        persisted.text = generated
        persisted.error = None
        persisted.finished_at = datetime.now(timezone.utc)
        if str(candidate.status or "new") != "new":
            persisted.status = "discarded"
            persisted.output = {**output_meta, "discard_reason": "candidate_not_active"}
            await self.session.commit()
            raise CandidateRewriteError("candidate is no longer active")
        if candidate_rewrite_input_hash(
            document,
            candidate,
            current_policy,
            variant,
        ) != input_hash:
            persisted.status = "stale"
            persisted.output = {**output_meta, "discard_reason": "source_or_policy_changed"}
            await self.session.commit()
            raise CandidateRewriteError("source or policy changed during rewrite")
        if (
            expected_run_id is not None
            and _candidate_current_rewrite_run_id(candidate) != expected_run_id
        ):
            persisted.status = "stale"
            persisted.output = {**output_meta, "discard_reason": "rewrite_authority_changed"}
            await self.session.commit()
            raise CandidateRewriteError("candidate rewrite authority changed during rewrite")

        persisted.status = "completed"
        persisted.output = output_meta
        candidate.meta = {
            **dict(candidate.meta or {}),
            "rewrite_run_id": int(persisted.id),
            "rewrite_provider": provider_name,
            "rewrite_model": model_name,
        }
        try:
            await self.session.commit()
            await self.session.refresh(persisted)
            await self.session.refresh(candidate)
            return CandidateRewriteResult(candidate, persisted, False)
        except Exception:
            await self.session.rollback()
            raise
