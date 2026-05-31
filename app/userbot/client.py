import socket
from base64 import b64encode
from urllib.parse import unquote, urlparse

from pyrogram import Client

from app.core.config import settings


def _patch_pysocks_http_auth_scheme() -> None:
    """Make PySocks emit `Proxy-Authorization: Basic ...`.

    PySocks 1.7.1 sends the HTTP proxy auth scheme as lowercase `basic`.
    Some providers reject that even though auth schemes are supposed to be
    case-insensitive, returning 407 Proxy Authentication Required.
    """
    import socks

    negotiate_http = getattr(socks.socksocket, "_negotiate_HTTP")
    if getattr(negotiate_http, "_basic_case_patched", False):
        return

    def _negotiate_HTTP(self, dest_addr, dest_port):  # noqa: ANN001, ANN202, N802
        _, _, _, rdns, username, password = self.proxy
        addr = dest_addr if rdns else socket.gethostbyname(dest_addr)

        http_headers = [
            b"CONNECT " + addr.encode("idna") + b":" + str(dest_port).encode() + b" HTTP/1.1",
            b"Host: " + dest_addr.encode("idna"),
        ]

        if username and password:
            http_headers.append(
                b"Proxy-Authorization: Basic " + b64encode(username + b":" + password)
            )

        http_headers.append(b"\r\n")
        self.sendall(b"\r\n".join(http_headers))

        fobj = self.makefile()
        status_line = fobj.readline()
        fobj.close()

        if not status_line:
            raise socks.GeneralProxyError("Connection closed unexpectedly")

        try:
            if isinstance(status_line, bytes):
                _, status_code, status_msg = status_line.split(b" ", 2)
            else:
                _, status_code, status_msg = status_line.split(" ", 2)
        except ValueError as exc:
            raise socks.GeneralProxyError("HTTP proxy server sent invalid response") from exc

        try:
            status_code = int(status_code)
        except ValueError as exc:
            raise socks.HTTPError("HTTP proxy server did not return a valid HTTP status") from exc

        if status_code != 200:
            if isinstance(status_msg, bytes):
                status_msg = status_msg.decode(errors="replace")
            error = f"{status_code}: {status_msg}"
            if status_code in (400, 403, 405):
                error += (
                    "\n[*] Note: The HTTP proxy server may not be supported by PySocks "
                    "(must be a CONNECT tunnel proxy)"
                )
            raise socks.HTTPError(error)

        self.proxy_sockname = (b"0.0.0.0", 0)
        self.proxy_peername = addr, dest_port

    setattr(_negotiate_HTTP, "_basic_case_patched", True)
    setattr(socks.socksocket, "_negotiate_HTTP", _negotiate_HTTP)
    getattr(socks.socksocket, "_proxy_negotiators")[socks.HTTP] = _negotiate_HTTP


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
        scheme = "http" if parsed.scheme == "https" else parsed.scheme
        proxy: dict = {
            "scheme": scheme,
            "hostname": parsed.hostname,
            "port": int(parsed.port),
        }
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    if settings.userbot_proxy_host and settings.userbot_proxy_port:
        scheme = settings.userbot_proxy_scheme or "socks5"
        proxy = {
            "scheme": "http" if scheme == "https" else scheme,
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
if USERBOT_PROXY and USERBOT_PROXY.get("scheme") == "http":
    _patch_pysocks_http_auth_scheme()

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
