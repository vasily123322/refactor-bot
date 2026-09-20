from __future__ import annotations

import asyncio

import httpx

from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig


def test_studio_cors_allows_patch_preflight_for_channel_dm_reply_intent(monkeypatch) -> None:
    async def run() -> None:
        origin = "https://frontend.example.test"
        monkeypatch.setenv("STUDIO_CORS_ORIGINS", origin)

        app = create_studio_app(StudioConfig.from_env())

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio.test"
        ) as client:
            response = await client.options(
                "/api/studio/channel-dm-reply-intents/123",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "PATCH",
                    "Access-Control-Request-Headers": (
                        "content-type,x-telegram-init-data"
                    ),
                },
            )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        allowed_methods = {
            method.strip()
            for method in response.headers["access-control-allow-methods"].split(",")
        }
        assert allowed_methods == {"GET", "POST", "PUT", "PATCH", "OPTIONS"}
        assert "*" not in allowed_methods

    asyncio.run(run())
