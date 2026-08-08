import asyncio

import pytest

from app.repositories.conversations import scoped_prompt_key
from app.services.http.fetcher import validate_public_http_url


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
