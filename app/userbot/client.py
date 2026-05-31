from urllib.parse import unquote, urlparse

from pyrogram import Client

from app.core.config import settings


def _build_proxy() -> dict | None:
    """Build Pyrogram proxy config from env settings.

    Supported forms:
    - USERBOT_PROXY_URL=socks5://user:pass@host:1080
    - USERBOT_PROXY_SCHEME=socks5 + USERBOT_PROXY_HOST + USERBOT_PROXY_PORT
    """
    if settings.userbot_proxy_url:
        parsed = urlparse(settings.userbot_proxy_url)
        if not parsed.scheme or not parsed.hostname or not parsed.port:
            raise ValueError(
                "USERBOT_PROXY_URL must look like socks5://host:port or socks5://user:pass@host:port"
            )
        proxy: dict = {
            "scheme": parsed.scheme,
            "hostname": parsed.hostname,
            "port": int(parsed.port),
        }
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    if settings.userbot_proxy_host and settings.userbot_proxy_port:
        proxy = {
            "scheme": settings.userbot_proxy_scheme or "socks5",
            "hostname": settings.userbot_proxy_host,
            "port": int(settings.userbot_proxy_port),
        }
        if settings.userbot_proxy_username:
            proxy["username"] = settings.userbot_proxy_username
        if settings.userbot_proxy_password:
            proxy["password"] = settings.userbot_proxy_password
        return proxy

    return None


USERBOT_PROXY = _build_proxy()

if settings.userbot_session:
    app = Client(
        name="userbot",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        session_string=settings.userbot_session,
        proxy=USERBOT_PROXY,
    )
else:
    # Фолбэк на файл-сессию ("userbot.session" в рабочем каталоге проекта)
    app = Client(
        name="userbot",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        proxy=USERBOT_PROXY,
    )
