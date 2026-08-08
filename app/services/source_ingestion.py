from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import SourceConnector
from app.repositories.sources_v2 import SourcesRepo
from app.services.http.fetcher import fetch_html


MAX_SOURCE_DOCUMENT_CHARS = 100_000
MAX_FEED_ENTRIES = 50


class SourceIngestionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IngestionResult:
    connector_id: int
    documents_seen: int
    documents_created: int
    candidates_created: int


FetchHtml = Callable[[str], Awaitable[object]]


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if self._ignored_depth == 0 and normalized in {
            "p",
            "br",
            "li",
            "h1",
            "h2",
            "h3",
            "blockquote",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        if self._ignored_depth == 0 and normalized in {
            "p",
            "li",
            "h1",
            "h2",
            "h3",
            "blockquote",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            value = data.strip()
            if value:
                self.parts.append(value)


def _bounded_text(value: str, *, limit: int = MAX_SOURCE_DOCUMENT_CHARS) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _clean_html(value: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    return _bounded_text(text)


def _payload_text(payload: object) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    if hasattr(payload, "text"):
        return str(getattr(payload, "text"))
    return str(payload)


def _first_text(node: ET.Element, names: set[str]) -> str | None:
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text and child.text.strip():
            return child.text.strip()
    return None


def _entry_link(node: ET.Element) -> str | None:
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local != "link":
            continue
        href = child.attrib.get("href")
        if href:
            rel = child.attrib.get("rel", "alternate")
            if rel in {"alternate", ""}:
                return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _rss_entries(xml_text: str, *, limit: int = MAX_FEED_ENTRIES) -> list[dict[str, Any]]:
    lowered = xml_text[:4096].lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise SourceIngestionError("RSS/Atom DTD and custom entities are not supported")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceIngestionError("RSS/Atom response is not valid XML") from exc

    nodes = [
        node
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}
    ]
    entries: list[dict[str, Any]] = []
    for node in nodes[: max(1, min(int(limit), MAX_FEED_ENTRIES))]:
        title = _first_text(node, {"title"})
        link = _entry_link(node)
        external_id = _first_text(node, {"guid", "id"}) or link
        body = _first_text(node, {"content", "encoded", "description", "summary"}) or ""
        content = _clean_html(body)
        if title and title not in content:
            content = _bounded_text(f"{title}\n\n{content}".strip())
        if not external_id:
            external_id = hashlib.sha256(
                f"{title or ''}\0{content}".encode("utf-8")
            ).hexdigest()
        published = _parse_date(
            _first_text(node, {"published", "updated", "pubdate", "date"})
        )
        entries.append(
            {
                "external_id": external_id,
                "source_url": link,
                "title": title,
                "content": content,
                "published_at": published,
            }
        )
    return entries


def _candidate_action(connector: SourceConnector) -> str:
    mode = str(connector.mode or "research")
    if mode == "summary":
        return "summarize"
    if mode == "rewrite":
        return "rewrite"
    if mode == "mirror":
        return "mirror" if connector.reuse_policy == "mirror_authorized" else "review"
    return "research"


class SourceIngestionService:
    def __init__(self, session: AsyncSession, *, fetcher: FetchHtml = fetch_html):
        self.session = session
        self.fetcher = fetcher
        self.repo = SourcesRepo(session)

    async def _mark_failure(self, connector: SourceConnector, reason: str) -> None:
        await self.repo.update_health(
            connector,
            status="broken",
            reason=reason,
            success=False,
        )

    async def ingest(self, connector: SourceConnector) -> IngestionResult:
        kind = str(connector.kind).lower()
        if not connector.enabled:
            return IngestionResult(int(connector.id), 0, 0, 0)
        if kind not in {"rss", "url", "web"}:
            raise SourceIngestionError(
                f"connector kind {kind!r} requires a dedicated ingestion adapter"
            )

        try:
            payload = await self.fetcher(str(connector.value))
        except Exception as exc:
            await self._mark_failure(
                connector,
                f"ingestion fetch failed: {type(exc).__name__}",
            )
            raise SourceIngestionError("source fetch failed") from exc

        raw = _payload_text(payload)
        try:
            if kind == "rss":
                entries = _rss_entries(raw)
            else:
                content = _clean_html(raw)
                if not content:
                    raise SourceIngestionError("web page produced no visible text")
                entries = [
                    {
                        "external_id": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        "source_url": str(connector.value),
                        "title": None,
                        "content": content,
                        "published_at": None,
                    }
                ]
        except SourceIngestionError as exc:
            await self._mark_failure(connector, str(exc))
            raise

        created_count = 0
        candidate_count = 0
        for entry in entries:
            document, created = await self.repo.upsert_document(
                connector=connector,
                external_id=str(entry["external_id"]),
                content=str(entry["content"]),
                source_url=entry.get("source_url"),
                title=entry.get("title"),
                published_at=entry.get("published_at"),
                metadata={
                    "connector_kind": kind,
                    "citation_enabled": bool(connector.citation_enabled),
                    "reuse_policy": str(connector.reuse_policy),
                },
            )
            if created:
                created_count += 1
                await self.repo.ensure_candidate(
                    source_document_id=document.id,
                    channel_id=connector.channel_id,
                    suggested_action=_candidate_action(connector),
                    metadata={
                        "source_connector_id": int(connector.id),
                        "reuse_policy": str(connector.reuse_policy),
                    },
                )
                candidate_count += 1

        if not entries:
            await self.repo.update_health(
                connector,
                status="degraded",
                reason="source returned no entries",
                success=False,
            )
        return IngestionResult(
            connector_id=int(connector.id),
            documents_seen=len(entries),
            documents_created=created_count,
            candidates_created=candidate_count,
        )
