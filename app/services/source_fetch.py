from __future__ import annotations

from urllib.parse import urlsplit

from loguru import logger

from app.services.extractors.html import extract_article_text
from app.services.http.fetcher import fetch_html


async def fetch_public_source_text(url: str, *, max_len: int = 2500) -> str:
    """Fetch a public URL/RSS source through the central SSRF-safe HTTP client."""
    try:
        html = await fetch_html(
            url,
            timeout_seconds=12.0,
            max_retries=1,
            user_agent="YubyBot/1.0",
        )
    except Exception as exc:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.hostname or '?'}"
        logger.warning("AI source fetch rejected/failed origin={} error={!r}", origin, exc)
        return ""

    return extract_article_text(html, max_len=max_len)
