from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from typing import Optional
from urllib.parse import urljoin

import httpx  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]

from app.core.url_security import validate_public_http_url


MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _should_retry_status(status_code: int) -> bool:
    return status_code == 429 or (500 <= status_code < 600)


def _compute_backoff(initial: float, max_backoff: float, attempt: int) -> float:
    delay = min(initial * (2 ** (attempt - 1)), max_backoff)
    jitter = random.uniform(0.0, 0.25 * delay)
    return delay + jitter


async def _get_with_safe_redirects(client: httpx.AsyncClient, url: str) -> httpx.Response:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        await validate_public_http_url(current_url)
        request = client.build_request("GET", current_url)
        response = await client.send(request, stream=True)
        if response.status_code not in _REDIRECT_STATUSES:
            return response

        location = response.headers.get("location")
        if not location:
            return response

        next_url = urljoin(str(response.request.url), location)
        await response.aclose()
        current_url = next_url

    raise RuntimeError("Слишком много HTTP redirect")


async def _read_limited_body(
    response: httpx.Response, *, limit: int = MAX_RESPONSE_BYTES
) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > limit:
            raise ValueError("Ответ страницы слишком большой")

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ValueError("Ответ страницы слишком большой")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_body(response: httpx.Response, body: bytes) -> str:
    encoding = response.encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


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
                try:
                    body = await _read_limited_body(resp)
                finally:
                    await resp.aclose()

            dur_ms = int((time.monotonic() - start_ts) * 1000)
            text = _decode_body(resp, body)

            if resp.is_success:
                logger.info(
                    f"HTTP fetch ok req_id={req_id} dur_ms={dur_ms} status={resp.status_code} bytes={len(body)}"
                )
                return text

            status = resp.status_code
            error_msg = f"HTTP {status}: {text[:200]}"
            if not _should_retry_status(status):
                logger.error(
                    f"HTTP fetch fail req_id={req_id} dur_ms={dur_ms} status={status}"
                )
                last_error = error_msg
                break

            last_error = error_msg
            logger.warning(
                f"HTTP fetch transient req_id={req_id} dur_ms={dur_ms} status={status} attempt={attempt}/{attempts}"
            )
        except ValueError as e:
            dur_ms = int((time.monotonic() - start_ts) * 1000)
            last_error = str(e)
            logger.warning(
                f"HTTP fetch rejected req_id={req_id} dur_ms={dur_ms} error={str(e)}"
            )
            break
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

    logger.error(f"HTTP fetch failed req_id={req_id}: {last_error}")
    raise RuntimeError(last_error or "fetch failed")
