from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.app as studio_app_module
from app.api.studio.app import create_studio_app
from app.api.studio.auth import validate_studio_init_data
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo


def _signed_init_data(
    *,
    user_id: int,
    auth_date: datetime,
    username: str = "studio_user",
    first_name: str = "Studio",
) -> str:
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
                "username": username,
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


def test_studio_init_data_validation_accepts_valid_signature_and_rejects_replay() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    raw = _signed_init_data(user_id=123, auth_date=now)

    principal = validate_studio_init_data(raw, now=now, max_age_seconds=60)
    assert principal.tg_user_id == 123
    assert principal.username == "studio_user"
    assert principal.full_name == "Studio"

    with pytest.raises(ValueError, match="expired"):
        validate_studio_init_data(
            raw,
            now=now + timedelta(minutes=2),
            max_age_seconds=60,
        )

    tampered = raw.replace("studio_user", "attacker")
    with pytest.raises(ValueError):
        validate_studio_init_data(tampered, now=now, max_age_seconds=60)


def test_studio_api_channel_scope_content_revisions_preview_and_schedule(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(1001, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -100123, "Main")
                other = await ClientsRepo(session).create_or_get(2002, "other", "Other")
                foreign_channel = await ChannelsRepo(session).create(
                    other.id, -100456, "Foreign"
                )

            now = datetime.now(timezone.utc).replace(microsecond=0)
            headers = {
                "X-Telegram-Init-Data": _signed_init_data(
                    user_id=1001,
                    auth_date=now,
                    username="owner",
                    first_name="Owner",
                )
            }
            app = create_studio_app(_studio_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://studio.test"
            ) as client:
                health = await client.get("/healthz")
                assert health.status_code == 200

                unauthorized = await client.get("/api/studio/channels")
                assert unauthorized.status_code == 401

                channels = await client.get("/api/studio/channels", headers=headers)
                assert channels.status_code == 200
                assert [row["id"] for row in channels.json()] == [channel.id]

                forbidden_as_not_found = await client.get(
                    f"/api/studio/channels/{foreign_channel.id}/content",
                    headers=headers,
                )
                assert forbidden_as_not_found.status_code == 404

                document_v1 = {
                    "schema_version": 1,
                    "mode": "classic",
                    "blocks": [{"id": "b1", "type": "text", "text": "Version one"}],
                    "telegram": {},
                    "metadata": {},
                }
                created = await client.post(
                    f"/api/studio/channels/{channel.id}/content",
                    headers=headers,
                    json={"title": "Draft", "document": document_v1},
                )
                assert created.status_code == 201
                content_id = created.json()["id"]
                assert created.json()["current_revision"] == 1

                document_v2 = {
                    **document_v1,
                    "blocks": [{"id": "b1", "type": "text", "text": "Version two"}],
                }
                revision = await client.post(
                    f"/api/studio/channels/{channel.id}/content/{content_id}/revisions",
                    headers=headers,
                    json={"document": document_v2, "status": "ready"},
                )
                assert revision.status_code == 200
                assert revision.json()["current_revision"] == 2
                assert revision.json()["status"] == "ready"
                assert revision.json()["document"]["blocks"][0]["text"] == "Version two"

                preview = await client.post(
                    "/api/studio/preview",
                    headers=headers,
                    json={"document": document_v2},
                )
                assert preview.status_code == 200
                assert preview.json()["publishable_via_legacy"] is True
                assert preview.json()["legacy_payload"]["text"] == "Version two"

                schedule = await client.post(
                    f"/api/studio/channels/{channel.id}/content/{content_id}/schedule",
                    headers=headers,
                    json={"repeat_seconds": 3600},
                )
                assert schedule.status_code == 201
                assert schedule.json()["status"] == "queued"
                assert schedule.json()["content_revision"] == 2
                assert "legacy_post_task_id" not in schedule.json()
        finally:
            await engine.dispose()

    asyncio.run(run())
