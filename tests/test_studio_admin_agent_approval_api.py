from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.admin_agent as admin_agent_api_module
import app.api.studio.app as studio_app_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAAPPROVAL",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Approval",
                "username": f"approval_{user_id}",
            },
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _config() -> StudioConfig:
    return StudioConfig(
        enabled=True,
        host="127.0.0.1",
        port=8080,
        public_url=None,
        init_data_max_age_seconds=86400,
        cors_origins=(),
    )


def test_studio_approval_routes_are_owner_scoped_bounded_and_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(admin_agent_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9951, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009951, "Owned")
                item = await ContentRepo(session).create(
                    channel_id=channel.id,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Draft"}]
                    ),
                    status="draft",
                    title="Очень длинный русский заголовок " * 5,
                    created_by_tg_user_id=owner.tg_user_id,
                )
                other = await ClientsRepo(session).create_or_get(9952, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009952, "Foreign")
                foreign_item = await ContentRepo(session).create(
                    channel_id=foreign.id,
                    document=PostDocument(
                        blocks=[{"id": "b2", "type": "text", "text": "Foreign"}]
                    ),
                    status="draft",
                    title="Foreign",
                    created_by_tg_user_id=other.tg_user_id,
                )

            headers = {"X-Telegram-Init-Data": _init_data(9951)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                forged = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals",
                    json={
                        "content_item_id": item.id,
                        "local_time": "14:30",
                        "request_id": "api-approval-0001",
                        "timezone": "Pacific/Auckland",
                        "target_local_date": "2099-01-01",
                        "state": "executed",
                    },
                    headers=headers,
                )
                assert forged.status_code == 422

                malformed = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals",
                    json={
                        "content_item_id": item.id,
                        "local_time": "29:90",
                        "request_id": "api-approval-0002",
                    },
                    headers=headers,
                )
                assert malformed.status_code == 422

                foreign_path = await client.post(
                    f"/api/studio/channels/{foreign.id}/assistant/approvals",
                    json={
                        "content_item_id": foreign_item.id,
                        "local_time": "14:30",
                        "request_id": "api-approval-0003",
                    },
                    headers=headers,
                )
                assert foreign_path.status_code == 404

                cross_content = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals",
                    json={
                        "content_item_id": foreign_item.id,
                        "local_time": "14:30",
                        "request_id": "api-approval-0004",
                    },
                    headers=headers,
                )
                assert cross_content.status_code == 422

                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals",
                    json={
                        "content_item_id": item.id,
                        "local_time": "14:30",
                        "request_id": "api-approval-0005",
                    },
                    headers=headers,
                )
                assert created.status_code == 201
                body = created.json()
                assert body["state"] == "pending_review"
                assert body["action_type"] == "schedule_draft_tomorrow"
                assert body["channel_id"] == channel.id
                assert body["content_item_id"] == item.id
                assert body["content_revision"] == 1
                assert body["timezone"] == "UTC+3"
                assert body["local_time"] == "14:30"
                assert body["schedule_entry_id"] is None
                assert body["publication_id"] is None

                retry = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals",
                    json={
                        "content_item_id": item.id,
                        "local_time": "16:00",
                        "request_id": "api-approval-0005",
                    },
                    headers=headers,
                )
                assert retry.status_code == 201
                assert retry.json()["id"] == body["id"]
                assert retry.json()["local_time"] == "14:30"

                listing = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/approvals",
                    headers=headers,
                )
                assert listing.status_code == 200
                assert [row["id"] for row in listing.json()] == [body["id"]]

                wrong_scope = await client.get(
                    f"/api/studio/channels/{foreign.id}/assistant/approvals/{body['id']}",
                    headers=headers,
                )
                assert wrong_scope.status_code == 404

                invalid_approve = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals/{body['id']}/approve",
                    json={"approve": True},
                    headers=headers,
                )
                assert invalid_approve.status_code == 422

                rejected = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals/{body['id']}/reject",
                    json={},
                    headers=headers,
                )
                assert rejected.status_code == 200
                assert rejected.json()["state"] == "rejected"

                rejected_retry = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals/{body['id']}/approve",
                    json={},
                    headers=headers,
                )
                assert rejected_retry.status_code == 200
                assert rejected_retry.json()["state"] == "rejected"

                executable = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals",
                    json={
                        "content_item_id": item.id,
                        "local_time": "15:45",
                        "request_id": "api-approval-0006",
                    },
                    headers=headers,
                )
                assert executable.status_code == 201
                executed = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals/{executable.json()['id']}/approve",
                    json={},
                    headers=headers,
                )
                assert executed.status_code == 200
                executed_body = executed.json()
                assert executed_body["state"] == "executed"
                assert executed_body["schedule_entry_id"] is not None
                assert executed_body["publication_id"] is not None

                second_approve = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/approvals/{executable.json()['id']}/approve",
                    json={},
                    headers=headers,
                )
                assert second_approve.status_code == 200
                assert second_approve.json()["schedule_entry_id"] == executed_body["schedule_entry_id"]
                assert second_approve.json()["publication_id"] == executed_body["publication_id"]

            async with Session() as session:
                assert (
                    await session.execute(
                        select(func.count(ScheduleEntry.id)).where(
                            ScheduleEntry.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 1
                assert (
                    await session.execute(
                        select(func.count(Publication.id)).where(
                            Publication.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
