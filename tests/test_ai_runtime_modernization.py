from __future__ import annotations

import asyncio

from app.repositories.conversations import ConversationsRepo, estimate_text_tokens
from app.services.ai_generation import AIGenerationService
from app.services.llm.openrouter_client import OpenRouterClient, _SharedHTTPPool


def test_openrouter_payload_uses_modern_completion_limit() -> None:
    client = OpenRouterClient()
    payload = client._build_payload(
        messages=[{"role": "user", "content": "hello"}],
        model="model",
        temperature=0.4,
        top_p=0.9,
        max_tokens=777,
    )
    assert payload["max_completion_tokens"] == 777
    assert "max_tokens" not in payload


def test_openrouter_usage_preserves_prompt_and_completion_breakdown() -> None:
    result = OpenRouterClient._result(
        success=True,
        text="ok",
        usage={"prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38},
    )
    assert result["prompt_tokens"] == 31
    assert result["completion_tokens"] == 7
    assert result["tokens_used"] == 38


def test_shared_http_pool_reuses_client_per_event_loop() -> None:
    async def run() -> None:
        first = _SharedHTTPPool.get()
        second = _SharedHTTPPool.get()
        assert first is second
        assert first.is_closed is False
        await OpenRouterClient.close_shared_http_clients()
        assert first.is_closed is True

    asyncio.run(run())


def test_shared_http_pool_recreates_client_after_shutdown() -> None:
    async def run() -> None:
        first = _SharedHTTPPool.get()
        await OpenRouterClient.close_shared_http_clients()
        second = _SharedHTTPPool.get()
        try:
            assert second is not first
            assert second.is_closed is False
        finally:
            await OpenRouterClient.close_shared_http_clients()

    asyncio.run(run())


def test_conversation_token_estimate_is_nonzero_and_deterministic() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcde") == 2
    assert estimate_text_tokens("x" * 400) == 100


def test_conversation_append_estimates_tokens_when_provider_count_missing() -> None:
    class _Session:
        def __init__(self) -> None:
            self.items = []

        def add(self, item) -> None:
            self.items.append(item)

        async def flush(self) -> None:
            return None

    async def run() -> None:
        session = _Session()
        repo = ConversationsRepo(session)  # type: ignore[arg-type]
        await repo.append(1, role="user", content="x" * 20, tokens=0)
        assert len(session.items) == 1
        assert session.items[0].tokens == 5

    asyncio.run(run())


def test_clear_history_scopes_prompt_key_to_channel() -> None:
    class _Repo:
        def __init__(self) -> None:
            self.args = None

        async def delete_by_user_and_prompt_key(self, **kwargs) -> int:
            self.args = kwargs
            return 1

    async def run() -> None:
        service = object.__new__(AIGenerationService)
        repo = _Repo()
        service.conv_repo = repo
        deleted = await service.clear_history(
            user_id=10,
            prompt_key="rewrite",
            channel_id=44,
        )
        assert deleted == 1
        assert repo.args == {
            "user_id": 10,
            "prompt_key": "rewrite",
            "channel_id": 44,
        }

    asyncio.run(run())
