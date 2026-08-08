import asyncio

import httpx
import pytest

from app.core.channel_access import _channel_id_from_callback
from app.repositories.conversations import scoped_prompt_key
from app.services.http.fetcher import _read_limited_body, validate_public_http_url


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
        ("neu_tags_12", 12),
        ("settings_neuropost_12", 12),
        ("unrelated_12", None),
    ],
)
def test_channel_callback_parser(
    callback_data: str, expected_channel_id: int | None
) -> None:
    assert _channel_id_from_callback(callback_data) == expected_channel_id


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


def test_streaming_body_limit_rejects_oversized_response() -> None:
    response = httpx.Response(200, content=b"x" * 16)
    with pytest.raises(ValueError, match="слишком большой"):
        asyncio.run(_read_limited_body(response, limit=8))
