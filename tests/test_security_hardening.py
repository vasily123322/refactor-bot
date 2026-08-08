import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.core.channel_access as channel_access
from app.core.channel_access import ChannelOwnerStateMiddleware, _channel_id_from_callback
from app.core.url_security import validate_public_http_url
from app.repositories.ai_settings import AISourcesRepo
from app.repositories.conversations import scoped_prompt_key
from app.services.http.fetcher import _read_limited_body


def test_scoped_prompt_key_separates_channels() -> None:
    key_a = scoped_prompt_key("auto_daily_topics", 1)
    key_b = scoped_prompt_key("auto_daily_topics", 2)

    assert key_a != key_b
    assert key_a.startswith("ch:1:")
    assert key_b.startswith("ch:2:")


def test_scoped_prompt_key_respects_database_limit() -> None:
    key = scoped_prompt_key("x" * 500, 123456)
    assert len(key) <= 64
    assert key.startswith("ch:123456:")


@pytest.mark.parametrize(
    ("callback_data", "expected_channel_id"),
    [
        ("ai_toggle_moderation_12", 12),
        ("ai_forbidden_clear_12", 12),
        ("ai_hashtags_count_12", 12),
        ("ai_set_hashtags_count_12_7", 12),
        ("ai_priority_12", 12),
        ("ai_priority_set_12_custom", 12),
        ("ai_source_add_12", 12),
        ("ai_source_digest_12", 12),
        ("ai_source_toggle_12_44", 12),
        ("ai_source_mode_12_44", 12),
        ("ai_source_delete_12_44", 12),
        ("source_draft_page_12_3", 12),
        ("source_draft_open_12_3", 12),
        ("neu_tags_12", 12),
        ("neu_sources_12", 12),
        ("settings_neuropost_12", 12),
        ("unrelated_12", None),
    ],
)
def test_channel_callback_parser(
    callback_data: str, expected_channel_id: int | None
) -> None:
    assert _channel_id_from_callback(callback_data) == expected_channel_id


class _FakeState:
    def __init__(self, data: dict[str, object]):
        self.data = dict(data)
        self.cleared = False

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def clear(self) -> None:
        self.cleared = True
        self.data.clear()


class _FakeMessage:
    def __init__(self, user_id: int):
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


async def _run_state_guard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state_data: dict[str, object],
    owns_channel: bool,
) -> tuple[object, _FakeState, _FakeMessage, int]:
    owner_checks = 0

    async def fake_user_owns_channel(*, user_id: int, channel_id: int) -> bool:
        nonlocal owner_checks
        owner_checks += 1
        assert user_id == 777
        if state_data.get("channel_id") is not None:
            try:
                assert channel_id == int(state_data["channel_id"])
            except (TypeError, ValueError):
                assert channel_id == 0
        return owns_channel

    monkeypatch.setattr(channel_access, "user_owns_channel", fake_user_owns_channel)

    state = _FakeState(state_data)
    message = _FakeMessage(777)
    calls = 0

    async def handler(event: object, data: dict[str, object]) -> str:
        nonlocal calls
        calls += 1
        return "handled"

    result = await ChannelOwnerStateMiddleware()(handler, message, {"state": state})
    return result, state, message, owner_checks if calls else -owner_checks


def test_fsm_guard_allows_messages_without_channel_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, state, message, check_marker = asyncio.run(
        _run_state_guard(monkeypatch, state_data={"preset_key": "x"}, owns_channel=False)
    )
    assert result == "handled"
    assert check_marker == 0
    assert not state.cleared
    assert message.answers == []


def test_fsm_guard_allows_channel_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    result, state, message, check_marker = asyncio.run(
        _run_state_guard(monkeypatch, state_data={"channel_id": 12}, owns_channel=True)
    )
    assert result == "handled"
    assert check_marker == 1
    assert not state.cleared
    assert message.answers == []


def test_fsm_guard_blocks_non_owner_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, state, message, check_marker = asyncio.run(
        _run_state_guard(monkeypatch, state_data={"channel_id": 12}, owns_channel=False)
    )
    assert result is None
    assert check_marker == -1
    assert state.cleared
    assert message.answers == ["Нет доступа к этому каналу. Действие отменено."]


def test_fsm_guard_blocks_invalid_channel_id(monkeypatch: pytest.MonkeyPatch) -> None:
    result, state, message, check_marker = asyncio.run(
        _run_state_guard(
            monkeypatch, state_data={"channel_id": "not-an-id"}, owns_channel=False
        )
    )
    assert result is None
    assert check_marker == -1
    assert state.cleared
    assert message.answers == ["Нет доступа к этому каналу. Действие отменено."]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
    ],
)
def test_ssrf_validator_rejects_local_and_non_http_urls(url: str) -> None:
    with pytest.raises(ValueError):
        asyncio.run(validate_public_http_url(url))


def test_ai_source_repo_rejects_private_url() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            AISourcesRepo._validate_source_value("url", "http://127.0.0.1/private")
        )


def test_ai_source_repo_allows_telegram_identifier_without_url_lookup() -> None:
    asyncio.run(AISourcesRepo._validate_source_value("telegram", "@public_channel"))


def test_streaming_body_limit_rejects_oversized_response() -> None:
    response = httpx.Response(200, content=b"x" * 16)
    with pytest.raises(ValueError, match="слишком большой"):
        asyncio.run(_read_limited_body(response, limit=8))
