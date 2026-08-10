from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.userbot.client import UserbotGateway


class FakeTelethonClient:
    def __init__(self, *, views=0, message_exists: bool = True) -> None:
        self.views = views
        self.message_exists = message_exists
        self.entity_calls: list[object] = []
        self.message_calls: list[tuple[object, int]] = []

    async def get_entity(self, target):
        self.entity_calls.append(target)
        return SimpleNamespace(id=123, username="channel")

    async def get_messages(self, entity, *, ids: int):
        self.message_calls.append((entity, int(ids)))
        if not self.message_exists:
            return None
        return SimpleNamespace(id=int(ids), views=self.views)


def _gateway(client: FakeTelethonClient) -> UserbotGateway:
    gateway = UserbotGateway.__new__(UserbotGateway)
    gateway._client = client  # type: ignore[assignment] - isolated transport seam
    return gateway


def test_get_message_views_reads_exact_message_without_increment_request() -> None:
    async def run() -> None:
        client = FakeTelethonClient(views=1234)
        gateway = _gateway(client)

        views = await gateway.get_message_views("@channel", 77)

        assert views == 1234
        assert client.entity_calls == ["channel"]
        assert len(client.message_calls) == 1
        assert client.message_calls[0][1] == 77

    asyncio.run(run())


def test_get_message_views_accepts_zero_and_fails_closed_for_bad_counts() -> None:
    async def run() -> None:
        client = FakeTelethonClient(views=0)
        gateway = _gateway(client)
        assert await gateway.get_message_views(-100123, 7) == 0

        for invalid in (None, True, -1, "many"):
            client.views = invalid
            assert await gateway.get_message_views(-100123, 7) is None

        client.message_exists = False
        assert await gateway.get_message_views(-100123, 7) is None

    asyncio.run(run())


def test_get_message_views_rejects_invalid_message_identity_before_network() -> None:
    async def run() -> None:
        client = FakeTelethonClient(views=100)
        gateway = _gateway(client)

        for invalid in (0, -1, True, "bad", None):
            assert await gateway.get_message_views(-100123, invalid) is None  # type: ignore[arg-type]

        assert client.entity_calls == []
        assert client.message_calls == []

    asyncio.run(run())
