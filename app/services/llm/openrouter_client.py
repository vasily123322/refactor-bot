from __future__ import annotations

import asyncio
import random
import httpx
from typing import List, Dict, Any, TypedDict, Literal
from loguru import logger
import contextlib
import time
import uuid


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatResult(TypedDict):
    success: bool
    text: str | None
    tokens_used: int
    error: str | None


class OpenRouterClient:
    """Минималистичный клиент для OpenRouter Chat Completions с ретраями.

    Возвращает унифицированный словарь: {success, text, tokens_used, error}.
    """

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

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/your-bot",
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
            "max_tokens": max_tokens,
            "reasoning": {"enabled": False},
        }

    def _should_retry_status(self, status_code: int) -> bool:
        # Транзиентные статусы: 429, 5xx
        return status_code == 429 or (500 <= status_code < 600)

    def _compute_backoff(self, attempt: int) -> float:
        # Экспоненциальный рост + лёгкий джиттер
        delay = min(self.backoff_initial * (2 ** (attempt - 1)), self.backoff_max)
        jitter = random.uniform(0.0, 0.25 * delay)
        return delay + jitter

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
        """Вызов OpenRouter /chat/completions с ретраями на транзиентные ошибки."""
        if not api_key:
            return {
                "success": False,
                "error": "OpenRouter API key не настроен",
                "text": None,
                "tokens_used": 0,
            }
        req_id = request_id or uuid.uuid4().hex
        url = f"{base_url}/chat/completions"
        headers = self._build_headers(api_key)
        payload = self._build_payload(
            messages=messages,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            start_ts = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    dur_ms = int((time.monotonic() - start_ts) * 1000)
                    if response.is_success:
                        data = response.json()
                        if "choices" not in data or not data["choices"]:
                            logger.info(
                                f"LLM call ok req_id={req_id} model={model} dur_ms={dur_ms} tokens=0 note=empty-choices"
                            )
                            return {
                                "success": False,
                                "error": "Пустой ответ от API",
                                "text": None,
                                "tokens_used": 0,
                            }
                        text = (data["choices"][0]["message"]["content"] or "").strip()
                        tokens_used = int(
                            data.get("usage", {}).get("total_tokens", 0) or 0
                        )
                        logger.info(
                            f"LLM call ok req_id={req_id} model={model} dur_ms={dur_ms} tokens={tokens_used}"
                        )
                        if tokens_used:
                            logger.info(
                                f"✅ OpenRouter: req_id={req_id} {tokens_used} токенов"
                            )
                        return {
                            "success": True,
                            "text": text,
                            "tokens_used": tokens_used,
                            "error": None,
                        }
                    # Неуспешный статус — решаем, ретраить ли
                    status = response.status_code
                    if not self._should_retry_status(status):
                        error_msg = f"HTTP {status}: {response.text}"
                        logger.error(
                            f"LLM call fail req_id={req_id} model={model} dur_ms={dur_ms} status={status} error=len({len(response.text)})"
                        )
                        logger.error(f"❌ OpenRouter HTTP error: {error_msg}")
                        return {
                            "success": False,
                            "error": error_msg,
                            "text": None,
                            "tokens_used": 0,
                        }
                    error_msg = f"HTTP {status}: {response.text}"
                    last_error = error_msg
                    logger.warning(
                        f"LLM call transient req_id={req_id} model={model} dur_ms={dur_ms} status={status} attempt={attempt}/{self.max_retries}"
                    )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                dur_ms = int((time.monotonic() - start_ts) * 1000)
                last_error = f"Транспортная ошибка: {str(e)}"
                logger.warning(
                    f"LLM call transport req_id={req_id} model={model} dur_ms={dur_ms} attempt={attempt}/{self.max_retries} error={str(e)}"
                )
            except Exception as e:
                dur_ms = int((time.monotonic() - start_ts) * 1000)
                last_error = f"Ошибка генерации: {str(e)}"
                logger.warning(
                    f"LLM call exception req_id={req_id} model={model} dur_ms={dur_ms} attempt={attempt}/{self.max_retries} error={str(e)}"
                )

            # Планируем ретрай, если есть попытки
            if attempt < self.max_retries:
                delay = self._compute_backoff(attempt)
                with contextlib.suppress(Exception):
                    await asyncio.sleep(delay)

        # Все попытки исчерпаны
        logger.error(f"❌ OpenRouter error after retries req_id={req_id}: {last_error}")
        return {
            "success": False,
            "error": (last_error or "Неизвестная ошибка"),
            "text": None,
            "tokens_used": 0,
        }
