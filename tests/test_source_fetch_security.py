from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import source_fetch
from app.services.http import fetcher


def test_source_fetch_uses_central_safe_client(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_fetch_html(url: str, **kwargs) -> str:
        calls.append((url, kwargs))
        return "<html><body><article><p>Public source text</p></article></body></html>"

    monkeypatch.setattr(source_fetch, "fetch_html", fake_fetch_html)

    text = asyncio.run(source_fetch.fetch_public_source_text("https://example.com/post"))

    assert "Public source text" in text
    assert calls == [
        (
            "https://example.com/post",
            {
                "timeout_seconds": 12.0,
                "max_retries": 1,
                "user_agent": "YubyBot/1.0",
            },
        )
    ]


def test_source_fetch_fails_closed_when_safe_client_rejects(monkeypatch):
    async def rejected(*args, **kwargs):
        raise ValueError("URL ведёт в приватную сеть")

    monkeypatch.setattr(source_fetch, "fetch_html", rejected)

    text = asyncio.run(source_fetch.fetch_public_source_text("http://127.0.0.1/admin"))

    assert text == ""


def test_safe_redirect_revalidates_private_target(monkeypatch):
    validated: list[str] = []
    requested: list[str] = []

    async def fake_validate(url: str) -> None:
        validated.append(url)
        if "127.0.0.1" in url:
            raise ValueError("URL ведёт в приватную сеть")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/internal"},
            request=request,
        )

    monkeypatch.setattr(fetcher, "validate_public_http_url", fake_validate)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="приватную сеть"):
                await fetcher._get_with_safe_redirects(client, "https://example.com/start")

    asyncio.run(run())

    assert validated == [
        "https://example.com/start",
        "http://127.0.0.1/internal",
    ]
    assert requested == ["https://example.com/start"]


def test_router_runtime_binds_safe_source_fetcher():
    from app.bot.routers import sources_module

    assert sources_module._fetch_url_source_text is source_fetch.fetch_public_source_text
