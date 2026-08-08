from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import random
import socket
import time
import uuid
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]


MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def _should_retry_status(status_code: int) -> bool:
    return status_code == 429 or (500 <= status_code < 600)


def _compute_backoff(initial: float, max_backoff: float, attempt: int) -> float:
    delay = min(initial * (2 ** (attempt - 1)), max_backoff)
    jitter = random.uniform(0.0, 0.25 * delay)
    return delay + jitter


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
    """Reject non-HTTP URLs and hosts that resolve to private/local addresses."""
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
        # If this is a literal IP, propagate the policy rejection. If it is a
        # hostname, resolve it below.
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


async def _get_with_safe_redirects(client: httpx.AsyncClient, url: str) -> httpx.Response:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        await validate_public_http_url(current_url)
        response = await client.get(current_url, follow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current_url = urljoin(str(response.request.url), location)
    raise RuntimeError("Слишком много HTTP redirect")


async def fetch_html(
    url: str,
    *,
    timeout_seconds: float = 30.0,
    max_retries: int = 2,
    backoff_initial: float = 0.4,
    backoff_max: float = 3.0,
    request_id: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    """Загрузить публичный HTML по URL с retry/backoff и SSRF-защитой."""
    req_id = request_id or uuid.uuid4().hex
    last_error: Optional[str] = None
    attempts = max(1, int(max_retries))

    for attempt in range(1, attempts + 1):
        start_ts = time.monotonic()
        try:
            headers = {"User-Agent": user_agent} if user_agent else None
            async with httpx.AsyncClient(
                timeout=float(timeout_seconds), headers=headers
            ) as client:
                resp = await _get_with_safe_redirects(client, url)
            dur_ms = int((time.monotonic() - start_ts) * 1000)

            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                raise ValueError("Ответ страницы слишком большой")
            if len(resp.content) > MAX_RESPONSE_BYTES:
                raise ValueError("Ответ страницы слишком большой")

            if resp.is_success:
                logger.info(
                    f"HTTP fetch ok req_id={req_id} dur_ms={dur_ms} status={resp.status_code}"
                )
                return resp.text

            status = resp.status_code
            if not _should_retry_status(status):
                error_msg = f"HTTP {status}: {resp.text[:200]}"
                logger.error(
                    f"HTTP fetch fail req_id={req_id} dur_ms={dur_ms} status={status}"
                )
                raise httpx.HTTPStatusError(
                    error_msg, request=resp.request, response=resp
                )
            last_error = f"HTTP {status}"
            logger.warning(
                f"HTTP fetch transient req_id={req_id} dur_ms={dur_ms} status={status} attempt={attempt}/{attempts}"
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            dur_ms = int((time.monotonic() - start_ts) * 1000)
            last_error = f"transport: {str(e)}"
            logger.warning(
                f"HTTP fetch transport req_id={req_id} dur_ms={dur_ms} attempt={attempt}/{attempts} error={str(e)}"
            )
        except Exception as e:
            dur_ms = int((time.monotonic() - start_ts) * 1000)
            last_error = str(e)
            logger.warning(
                f"HTTP fetch exception req_id={req_id} dur_ms={dur_ms} attempt={attempt}/{attempts} error={str(e)}"
            )

        if attempt < attempts:
            delay = _compute_backoff(
                float(backoff_initial), float(backoff_max), attempt
            )
            with contextlib.suppress(Exception):
                await asyncio.sleep(delay)

    logger.error(f"HTTP fetch failed after retries req_id={req_id}: {last_error}")
    raise RuntimeError(last_error or "fetch failed")
