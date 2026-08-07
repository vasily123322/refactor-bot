from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from app.core.config import settings


def normalize_aiohttp_proxy_url(url: str | None) -> str | None:
    """Return a proxy URL suitable for aiohttp/aiogram Bot API requests.

    Providers sometimes label HTTP CONNECT proxies as ``https://user:pass@host:port``.
    aiohttp treats that as TLS-to-proxy and fails with SSL record layer errors for
    these proxies, so for Telegram HTTPS targets we must pass an ``http://`` proxy.
    """
    if not url:
        return None
    value = url.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme == "https":
        parsed = parsed._replace(scheme="http")
        return urlunparse(parsed)
    return value


def get_bot_api_proxy_url() -> str | None:
    """Proxy for aiogram Bot API traffic.

    BOT_PROXY_URL can override it; otherwise reuse USERBOT_PROXY_URL so the main
    bot and external bots are protected from direct Telegram network timeouts.
    """
    return normalize_aiohttp_proxy_url(settings.bot_proxy_url or settings.userbot_proxy_url)
