from __future__ import annotations

from scripts.secret_scan import scan_text


def _rules(text: str) -> set[str]:
    return {finding.rule for finding in scan_text("fixture.py", text)}


def test_scanner_detects_telegram_token() -> None:
    token = "987654321:" + "A" * 36
    assert "telegram-bot-token" in _rules(f'BOT_TOKEN="{token}"')


def test_scanner_detects_sk_style_api_key() -> None:
    key = "sk-" + "Z" * 32
    assert "sk-api-key" in _rules(f'OPENAI_API_KEY="{key}"')


def test_scanner_detects_private_key_header() -> None:
    header = "-----BEGIN " + "PRIVATE KEY-----"
    assert "private-key" in _rules(header)


def test_scanner_detects_secret_interpolation_in_output_text() -> None:
    placeholder = "{" + "token" + "}"
    source = (
        'text_log = f"пользователь добавил бот с токеном: <code>'
        + placeholder
        + '</code>"'
    )
    assert "credential-output-interpolation" in _rules(source)


def test_scanner_allows_normal_runtime_references() -> None:
    source = "bot = Bot(token=settings.bot_token)\ncallback_data = f'settings_post_{channel_id}'"
    assert scan_text("fixture.py", source) == []
