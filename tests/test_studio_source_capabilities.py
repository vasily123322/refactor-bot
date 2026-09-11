from __future__ import annotations

import asyncio

from fastapi.routing import APIRoute

from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig


def _studio_config() -> StudioConfig:
    return StudioConfig(
        enabled=True,
        host="127.0.0.1",
        port=8080,
        public_url="https://studio.example.test",
        init_data_max_age_seconds=86400,
        cors_origins=(),
    )


def test_capabilities_advertise_all_supported_source_create_kinds() -> None:
    app = create_studio_app(_studio_config())
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/api/studio/capabilities"
    )

    payload = asyncio.run(route.endpoint(None))

    assert payload["source_kinds"] == [
        "telegram",
        "rss",
        "url",
        "telegram_channel_dms",
    ]
