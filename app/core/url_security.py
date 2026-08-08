from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_public_http_url(url: str) -> None:
    """Reject URLs that can address local/private network resources."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Разрешены только http/https URL")
    if not parsed.hostname:
        raise ValueError("URL не содержит hostname")
    if parsed.username or parsed.password:
        raise ValueError("URL с учётными данными не поддерживаются")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Локальные адреса запрещены")

    try:
        if not _is_public_ip(hostname):
            raise ValueError("Приватные и локальные IP запрещены")
        return
    except ValueError as exc:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise exc

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not infos:
        raise ValueError("Не удалось разрешить hostname")
    for info in infos:
        address = info[4][0]
        if not _is_public_ip(address):
            raise ValueError("Hostname указывает на приватный или локальный IP")
