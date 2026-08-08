from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.join_query_decisions import (
    answer_join_query,
    resolve_immediate_join_decision,
)


class _Bot:
    def __init__(self, *, query_fails: bool = False) -> None:
        self.query_fails = query_fails
        self.query_calls: list[tuple[str, str]] = []
        self.approve_calls: list[tuple[int, int]] = []
        self.decline_calls: list[tuple[int, int]] = []

    async def answer_chat_join_request_query(
        self, *, chat_join_request_query_id: str, result: str
    ) -> None:
        self.query_calls.append((chat_join_request_query_id, result))
        if self.query_fails:
            raise RuntimeError("query unavailable")

    async def approve_chat_join_request(self, *, chat_id: int, user_id: int) -> None:
        self.approve_calls.append((chat_id, user_id))

    async def decline_chat_join_request(self, *, chat_id: int, user_id: int) -> None:
        self.decline_calls.append((chat_id, user_id))


def _event(bot: _Bot, *, query_id: str | None):
    return SimpleNamespace(
        bot=bot,
        query_id=query_id,
        chat=SimpleNamespace(id=-100500),
        from_user=SimpleNamespace(id=77),
    )


def test_query_approve_uses_bot_api_10_1_path_only() -> None:
    async def run() -> None:
        bot = _Bot()
        ok = await resolve_immediate_join_decision(
            _event(bot, query_id="query-1"), "approve"
        )
        assert ok is True
        assert bot.query_calls == [("query-1", "approve")]
        assert bot.approve_calls == []

    asyncio.run(run())


def test_query_decline_uses_bot_api_10_1_path_only() -> None:
    async def run() -> None:
        bot = _Bot()
        ok = await resolve_immediate_join_decision(
            _event(bot, query_id="query-2"), "decline"
        )
        assert ok is True
        assert bot.query_calls == [("query-2", "decline")]
        assert bot.decline_calls == []

    asyncio.run(run())


def test_legacy_decline_falls_back_to_classic_join_api() -> None:
    async def run() -> None:
        bot = _Bot()
        ok = await resolve_immediate_join_decision(
            _event(bot, query_id=None), "decline"
        )
        assert ok is True
        assert bot.query_calls == []
        assert bot.decline_calls == [(-100500, 77)]

    asyncio.run(run())


def test_failed_query_answer_falls_back_for_immediate_decision() -> None:
    async def run() -> None:
        bot = _Bot(query_fails=True)
        ok = await resolve_immediate_join_decision(
            _event(bot, query_id="expired-query"), "approve"
        )
        assert ok is True
        assert bot.query_calls == [("expired-query", "approve")]
        assert bot.approve_calls == [(-100500, 77)]

    asyncio.run(run())


def test_queue_is_only_sent_when_query_id_exists() -> None:
    async def run() -> None:
        modern_bot = _Bot()
        assert await answer_join_query(
            _event(modern_bot, query_id="queue-query"), "queue"
        ) is True
        assert modern_bot.query_calls == [("queue-query", "queue")]

        legacy_bot = _Bot()
        assert await answer_join_query(_event(legacy_bot, query_id=None), "queue") is False
        assert legacy_bot.query_calls == []

    asyncio.run(run())
