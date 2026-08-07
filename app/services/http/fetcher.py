from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from typing import Optional

import httpx  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]


def _should_retry_status(status_code: int) -> bool:
    # 429 и 5xx считаем транзиентными
    return status_code == 429 or (500 <= status_code < 600)


def _compute_backoff(initial: float, max_backoff: float, attempt: int) -> float:
    delay = min(initial * (2 ** (attempt - 1)), max_backoff)
    jitter = random.uniform(0.0, 0.25 * delay)
    return delay + jitter


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
    """Загрузить HTML по URL с ретраями и экспоненциальным backoff.

    Возвращает текст HTML или бросает исключение после исчерпания попыток.
    """
    req_id = request_id or uuid.uuid4().hex
    last_error: Optional[str] = None
    for attempt in range(1, max(1, int(max_retries)) + 1):
        start_ts = time.monotonic()
        try:
            headers = {"User-Agent": user_agent} if user_agent else None
            async with httpx.AsyncClient(
                timeout=float(timeout_seconds), headers=headers
            ) as client:
                resp = await client.get(url, follow_redirects=True)
            dur_ms = int((time.monotonic() - start_ts) * 1000)
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
                f"HTTP fetch transient req_id={req_id} dur_ms={dur_ms} status={status} attempt={attempt}/{max_retries}"
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            dur_ms = int((time.monotonic() - start_ts) * 1000)
            last_error = f"transport: {str(e)}"
            logger.warning(
                f"HTTP fetch transport req_id={req_id} dur_ms={dur_ms} attempt={attempt}/{max_retries} error={str(e)}"
            )
        except Exception as e:
            dur_ms = int((time.monotonic() - start_ts) * 1000)
            last_error = str(e)
            logger.warning(
                f"HTTP fetch exception req_id={req_id} dur_ms={dur_ms} attempt={attempt}/{max_retries} error={str(e)}"
            )

        # планируем ретрай, если есть попытки
        if attempt < max_retries:
            delay = _compute_backoff(
                float(backoff_initial), float(backoff_max), attempt
            )
            with contextlib.suppress(Exception):
                await asyncio.sleep(delay)

    logger.error(f"HTTP fetch failed after retries req_id={req_id}: {last_error}")
    raise RuntimeError(last_error or "fetch failed")
