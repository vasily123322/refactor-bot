from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
import uuid
import weakref
from typing import Any, AsyncIterator, Dict, List, Literal, TypedDict

import httpx
from loguru import logger


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatResult(TypedDict):
    success: bool
    text: str | None
    tokens_used: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None


class ChatStreamEvent(TypedDict, total=False):
    type: Literal["delta", "done", "error"]
    text: str
    result: ChatResult


class _SharedHTTPPool:
    """Reuse HTTP connections while keeping clients bound to their event loop."""

    _clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
        weakref.WeakKeyDictionary()
    )

    @classmethod
    def get(cls) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = cls._clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=None,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                    keepalive_expiry=30.0,
                ),
            )
            cls._clients[loop] = client
        return client

    @classmethod
    async def close_all(cls) -> None:
        clients = list(cls._clients.values())
        cls._clients.clear()
        for client in clients:
            if not client.is_closed:
                with contextlib.suppress(Exception):
                    await client.aclose()


class OpenRouterClient:
    """OpenRouter Chat Completions client with retries and pooled HTTP transport."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        backoff_initial: float = 0.5,
        backoff_max: float = 5.0,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(1, int(max_retries))
        self.backoff_initial = max(0.0, float(backoff_initial))
        self.backoff_max = max(self.backoff_initial, float(backoff_max))

    @staticmethod
    async def close_shared_http_clients() -> None:
        await _SharedHTTPPool.close_all()

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "refactor-bot",
        }

    def _build_payload(
        self,
        *,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        return {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_completion_tokens": max_tokens,
            "reasoning": {"enabled": False},
        }

    @staticmethod
    def _result(
        *,
        success: bool,
        text: str | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> ChatResult:
        usage = usage or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0)
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens
        return {
            "success": success,
            "text": text,
            "tokens_used": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "error": error,
        }

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | None:
        """Parse one OpenRouter SSE data line and ignore keepalive comments."""
        value = (line or "").strip()
        if not value or value.startswith(":") or not value.startswith("data:"):
            return None
        payload = value[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("OpenRouter stream: ignored malformed SSE payload")
            return None
        return decoded if isinstance(decoded, dict) else None

    def _should_retry_status(self, status_code: int) -> bool:
        return status_code == 429 or (500 <= status_code < 600)

    def _compute_backoff(self, attempt: int) -> float:
        delay = min(self.backoff_initial * (2 ** (attempt - 1)), self.backoff_max)
        jitter = random.uniform(0.0, 0.25 * delay)
        return delay + jitter

    @staticmethod
    def _response_error(status: int, body: str) -> str:
        compact = " ".join((body or "").split())
        if len(compact) > 500:
            compact = compact[:497] + "..."
        return f"HTTP {status}: {compact}" if compact else f"HTTP {status}"

    @staticmethod
    def _stream_error_message(payload: dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "stream error")
        else:
            message = str(error or "stream error")
        return f"OpenRouter stream error: {message}"

    async def chat(
        self,
        *,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        base_url: str,
        api_key: str,
        request_id: str | None = None,
    ) -> ChatResult:
        """Call OpenRouter /chat/completions with retry on transient failures."""
        if not api_key:
            return self._result(success=False, error="OpenRouter API key не настроен")

        req_id = request_id or uuid.uuid4().hex
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = self._build_headers(api_key)
        payload = self._build_payload(
            messages=messages,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            start_ts = time.monotonic()
            try:
                response = await _SharedHTTPPool.get().post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                dur_ms = int((time.monotonic() - start_ts) * 1000)
                if response.is_success:
                    data = response.json()
                    choices = data.get("choices") or []
                    if not choices:
                        logger.info(
                            "LLM call empty req_id={} model={} dur_ms={}",
                            req_id,
                            model,
                            dur_ms,
                        )
                        return self._result(success=False, error="Пустой ответ от API")

                    text = (
                        choices[0].get("message", {}).get("content") or ""
                    ).strip()
                    result = self._result(
                        success=True,
                        text=text,
                        usage=data.get("usage") or {},
                    )
                    logger.info(
                        "LLM call ok req_id={} model={} dur_ms={} prompt_tokens={} completion_tokens={} total_tokens={}",
                        req_id,
                        model,
                        dur_ms,
                        result["prompt_tokens"],
                        result["completion_tokens"],
                        result["tokens_used"],
                    )
                    return result

                status = response.status_code
                error_msg = self._response_error(status, response.text)
                if not self._should_retry_status(status):
                    logger.error(
                        "LLM call fail req_id={} model={} dur_ms={} status={}",
                        req_id,
                        model,
                        dur_ms,
                        status,
                    )
                    return self._result(success=False, error=error_msg)

                last_error = error_msg
                logger.warning(
                    "LLM call transient req_id={} model={} dur_ms={} status={} attempt={}/{}",
                    req_id,
                    model,
                    dur_ms,
                    status,
                    attempt,
                    self.max_retries,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                dur_ms = int((time.monotonic() - start_ts) * 1000)
                last_error = f"Транспортная ошибка: {exc}"
                logger.warning(
                    "LLM transport req_id={} model={} dur_ms={} attempt={}/{} error={!r}",
                    req_id,
                    model,
                    dur_ms,
                    attempt,
                    self.max_retries,
                    exc,
                )
            except Exception as exc:
                dur_ms = int((time.monotonic() - start_ts) * 1000)
                last_error = f"Ошибка генерации: {exc}"
                logger.exception(
                    "LLM exception req_id={} model={} dur_ms={} attempt={}/{}",
                    req_id,
                    model,
                    dur_ms,
                    attempt,
                    self.max_retries,
                )

            if attempt < self.max_retries:
                await asyncio.sleep(self._compute_backoff(attempt))

        logger.error("LLM error after retries req_id={} model={}", req_id, model)
        return self._result(
            success=False,
            error=last_error or "Неизвестная ошибка",
        )

    async def stream_chat(
        self,
        *,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        base_url: str,
        api_key: str,
        request_id: str | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        """Stream OpenRouter SSE deltas and emit one terminal result event.

        Transient failures are retried only before the first user-visible delta.
        Once partial text has been emitted, retrying would duplicate content, so a
        mid-stream error terminates the stream with the partial text attached.
        """
        if not api_key:
            yield {
                "type": "error",
                "result": self._result(
                    success=False, error="OpenRouter API key не настроен"
                ),
            }
            return

        req_id = request_id or uuid.uuid4().hex
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = self._build_headers(api_key)
        payload = self._build_payload(
            messages=messages,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )
        payload["stream"] = True

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            emitted = False
            chunks: list[str] = []
            usage: dict[str, Any] = {}
            start_ts = time.monotonic()
            try:
                async with _SharedHTTPPool.get().stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                ) as response:
                    if not response.is_success:
                        raw = await response.aread()
                        body = raw.decode(errors="replace")
                        status = response.status_code
                        last_error = self._response_error(status, body)
                        if not self._should_retry_status(status):
                            yield {
                                "type": "error",
                                "result": self._result(
                                    success=False, error=last_error
                                ),
                            }
                            return
                    else:
                        stream_failed_before_delta = False
                        async for line in response.aiter_lines():
                            data = self._parse_sse_line(line)
                            if data is None:
                                continue

                            if data.get("usage"):
                                usage = dict(data.get("usage") or {})

                            if data.get("error"):
                                last_error = self._stream_error_message(data)
                                if emitted:
                                    partial = "".join(chunks)
                                    yield {
                                        "type": "error",
                                        "result": self._result(
                                            success=False,
                                            text=partial,
                                            error=last_error,
                                            usage=usage,
                                        ),
                                    }
                                    return
                                stream_failed_before_delta = True
                                break

                            choices = data.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                emitted = True
                                chunks.append(content)
                                yield {"type": "delta", "text": content}

                        if not stream_failed_before_delta:
                            full_text = "".join(chunks).strip()
                            if full_text:
                                result = self._result(
                                    success=True,
                                    text=full_text,
                                    usage=usage,
                                )
                                dur_ms = int((time.monotonic() - start_ts) * 1000)
                                logger.info(
                                    "LLM stream ok req_id={} model={} dur_ms={} prompt_tokens={} completion_tokens={} total_tokens={}",
                                    req_id,
                                    model,
                                    dur_ms,
                                    result["prompt_tokens"],
                                    result["completion_tokens"],
                                    result["tokens_used"],
                                )
                                yield {"type": "done", "result": result}
                                return
                            last_error = "Пустой ответ от API"

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"Транспортная ошибка: {exc}"
                if emitted:
                    yield {
                        "type": "error",
                        "result": self._result(
                            success=False,
                            text="".join(chunks),
                            error=last_error,
                            usage=usage,
                        ),
                    }
                    return
                logger.warning(
                    "LLM stream transport req_id={} model={} attempt={}/{} error={!r}",
                    req_id,
                    model,
                    attempt,
                    self.max_retries,
                    exc,
                )
            except Exception as exc:
                last_error = f"Ошибка генерации: {exc}"
                if emitted:
                    yield {
                        "type": "error",
                        "result": self._result(
                            success=False,
                            text="".join(chunks),
                            error=last_error,
                            usage=usage,
                        ),
                    }
                    return
                logger.exception(
                    "LLM stream exception req_id={} model={} attempt={}/{}",
                    req_id,
                    model,
                    attempt,
                    self.max_retries,
                )

            if attempt < self.max_retries:
                await asyncio.sleep(self._compute_backoff(attempt))

        yield {
            "type": "error",
            "result": self._result(
                success=False,
                error=last_error or "Неизвестная ошибка",
            ),
        }
