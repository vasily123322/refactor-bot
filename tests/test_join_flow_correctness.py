from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from aiogram import Bot, Dispatcher

import app.bot.routers.shared_join as shared_join
from app.bot.routers.shared_join import build_shared_join_router
from app.services.external_bots import ExternalBotsManager
from app.services.join_requests import JoinRequestsService


class _FakeSession:
    def __init__(self, calls: dict[str, int] | None = None) -> None:
        self.calls = calls or {}
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        self.calls["execute"] = self.calls.get("execute", 0) + 1
        raise AssertionError("generic DM handler consumed /start")

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeChannelsRepoForRouting:
    calls: dict[str, int] = {}

    def __init__(self, session) -> None:
        self.session = session

    async def get_by_id(self, channel_id: int):
        self.calls["get_by_id"] = self.calls.get("get_by_id", 0) + 1
        return None


def test_start_deeplink_bypasses_generic_text_handler(monkeypatch) -> None:
    calls: dict[str, int] = {}
    _FakeChannelsRepoForRouting.calls = calls
    monkeypatch.setattr(shared_join, "AsyncSessionLocal", lambda: _FakeSession(calls))
    monkeypatch.setattr(shared_join, "ChannelsRepo", _FakeChannelsRepoForRouting)

    async def run() -> None:
        bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        dp = Dispatcher()
        dp.include_router(build_shared_join_router(7))
        try:
            await dp.feed_raw_update(
                bot=bot,
                update={
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": int(time.time()),
                        "text": "/start join_c_42_campaign",
                        "chat": {"id": 100, "type": "private"},
                        "from": {
                            "id": 100,
                            "is_bot": False,
                            "first_name": "Test",
                        },
                    },
                },
            )
        finally:
            await bot.session.close()

    asyncio.run(run())
    assert calls.get("get_by_id") == 1
    assert calls.get("execute", 0) == 0


def test_captcha_payload_contains_solvable_challenge() -> None:
    payload = JoinRequestsService._build_challenge_payload(2)
    assert payload is not None
    assert 2 <= payload["a"] <= 9
    assert 2 <= payload["b"] <= 9
    assert payload["answer"] == str(payload["a"] + payload["b"])
    assert JoinRequestsService._build_challenge_payload(1) is None
    assert JoinRequestsService._build_challenge_payload(3) is None


class _FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.approved: list[tuple[int, int]] = []

    async def approve_chat_join_request(self, *, chat_id: int, user_id: int) -> None:
        if self.fail:
            raise RuntimeError("telegram failure")
        self.approved.append((chat_id, user_id))


class _FakeChannelsRepo:
    async def get_by_id(self, channel_id: int):
        return SimpleNamespace(tg_chat_id=-100123)


class _FakeSubscribersRepo:
    def __init__(self) -> None:
        self.added: list[tuple[int, int]] = []
        self.tags: list[tuple[int, int, str]] = []

    async def add(self, channel_id: int, user_id: int, username, full_name) -> None:
        self.added.append((channel_id, user_id))

    async def add_tag(self, channel_id: int, user_id: int, tag: str) -> None:
        self.tags.append((channel_id, user_id, tag))


class _FakeJoinRequestsRepo:
    def __init__(self) -> None:
        self.statuses: list[tuple[int, int, str]] = []

    async def set_status(self, channel_id: int, user_id: int, status: str) -> None:
        self.statuses.append((channel_id, user_id, status))


def test_delayed_approver_persists_each_successful_request() -> None:
    async def run() -> None:
        manager = ExternalBotsManager()
        bot = _FakeBot()
        manager._bots[5] = bot  # type: ignore[assignment]
        session = _FakeSession()
        subs = _FakeSubscribersRepo()
        requests = _FakeJoinRequestsRepo()
        jr = SimpleNamespace(
            challenge_type="captcha",
            attempts_left=0,
            user_id=77,
            challenge_payload={"utm": "campaign"},
        )

        ok = await manager._approve_pending_item(
            5,
            9,
            jr,
            ch_repo=_FakeChannelsRepo(),
            subs_repo=subs,
            jr_repo=requests,
            session=session,
        )

        assert ok is True
        assert bot.approved == [(-100123, 77)]
        assert subs.added == [(9, 77)]
        assert subs.tags == [(9, 77, "campaign")]
        assert requests.statuses == [(9, 77, "approved")]
        assert session.commits == 1
        assert session.rollbacks == 0

    asyncio.run(run())


def test_delayed_approver_does_not_mark_failed_telegram_approval() -> None:
    async def run() -> None:
        manager = ExternalBotsManager()
        manager._bots[5] = _FakeBot(fail=True)  # type: ignore[assignment]
        session = _FakeSession()
        subs = _FakeSubscribersRepo()
        requests = _FakeJoinRequestsRepo()
        jr = SimpleNamespace(
            challenge_type=None,
            attempts_left=None,
            user_id=88,
            challenge_payload={},
        )

        ok = await manager._approve_pending_item(
            5,
            9,
            jr,
            ch_repo=_FakeChannelsRepo(),
            subs_repo=subs,
            jr_repo=requests,
            session=session,
        )

        assert ok is False
        assert subs.added == []
        assert requests.statuses == []
        assert session.commits == 0

    asyncio.run(run())
