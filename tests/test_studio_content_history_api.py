from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.app as studio_app_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo


def _signed_init_data(*, user_id: int, auth_date: datetime) -> str:
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "History",
                "username": "history",
                "language_code": "en",
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    }
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=settings.bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()
    values["hash"] = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return urlencode(values)


def _studio_config() -> StudioConfig:
    return StudioConfig(
        enabled=True,
        host="127.0.0.1",
        port=8080,
        public_url="https://studio.example.test",
        init_data_max_age_seconds=86400,
        cors_origins=(),
    )


def test_studio_content_history_cursor_is_deterministic(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)

            same_updated_at = datetime(2026, 9, 19, 12, 0, 0)
            document = {
                "schema_version": 1,
                "mode": "classic",
                "blocks": [{"id": "b1", "type": "text", "text": "History item"}],
                "telegram": {},
                "metadata": {},
            }
            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(3003, "history", "History")
                channel = await ChannelsRepo(session).create(owner.id, -100789, "History")
                rows = await ContentRepo(session).create_batch(
                    channel_id=channel.id,
                    items=[
                        {"title": f"Item {index}", "document": document}
                        for index in range(4)
                    ],
                    created_by_tg_user_id=owner.tg_user_id,
                    source="test",
                )
                for row in rows:
                    row.updated_at = same_updated_at
                await session.commit()
                expected_ids = sorted((int(row.id) for row in rows), reverse=True)

            headers = {
                "X-Telegram-Init-Data": _signed_init_data(
                    user_id=3003,
                    auth_date=datetime.now(timezone.utc).replace(microsecond=0),
                )
            }
            app = create_studio_app(_studio_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://studio.test",
            ) as client:
                first = await client.get(
                    f"/api/studio/channels/{channel.id}/content",
                    headers=headers,
                    params={"limit": 2},
                )
                assert first.status_code == 200
                first_rows = first.json()
                assert [row["id"] for row in first_rows] == expected_ids[:2]

                cursor = first_rows[-1]
                second = await client.get(
                    f"/api/studio/channels/{channel.id}/content",
                    headers=headers,
                    params={
                        "limit": 2,
                        "before_updated_at": cursor["updated_at"],
                        "before_id": cursor["id"],
                    },
                )
                assert second.status_code == 200
                assert [row["id"] for row in second.json()] == expected_ids[2:]

                incomplete_cursor = await client.get(
                    f"/api/studio/channels/{channel.id}/content",
                    headers=headers,
                    params={"before_id": expected_ids[0]},
                )
                assert incomplete_cursor.status_code == 422
                assert "provided together" in incomplete_cursor.json()["detail"]
        finally:
            await engine.dispose()

    asyncio.run(run())
