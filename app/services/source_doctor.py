from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from app.domain.sources.models import SourceConnector
from app.services.http.fetcher import fetch_html


@dataclass(frozen=True, slots=True)
class SourceHealthResult:
    status: str
    reason: str | None
    auth_state: str
    capabilities: dict[str, Any]
    health: dict[str, Any]
    success: bool = False


WebProbe = Callable[[str], Awaitable[object]]
TelegramProbe = Callable[[str], Awaitable[object]]


class SourceDoctor:
    """Capability/health checker isolated from source business logic."""

    def __init__(
        self,
        *,
        web_probe: WebProbe = fetch_html,
        telegram_probe: TelegramProbe | None = None,
    ):
        self.web_probe = web_probe
        self.telegram_probe = telegram_probe

    @staticmethod
    def capabilities(kind: str) -> dict[str, Any]:
        normalized = str(kind).lower()
        if normalized == "telegram":
            return {
                "history": True,
                "live": True,
                "media": True,
                "auth": "mtproto_session",
                "browser": False,
            }
        if normalized == "telegram_channel_dms":
            return {
                "history": False,
                "live": True,
                "media": True,
                "auth": "bot_api",
                "browser": False,
            }
        if normalized == "rss":
            return {
                "history": True,
                "live": False,
                "media": "enclosures",
                "auth": "none",
                "browser": False,
            }
        if normalized in {"url", "web"}:
            return {
                "history": False,
                "live": False,
                "media": "page",
                "auth": "none",
                "browser": False,
            }
        if normalized == "local_browser":
            return {
                "history": False,
                "live": False,
                "media": True,
                "auth": "local_session",
                "browser": True,
            }
        return {
            "history": False,
            "live": False,
            "media": False,
            "auth": "unknown",
            "browser": False,
        }

    async def check(self, connector: SourceConnector) -> SourceHealthResult:
        if not connector.enabled:
            return SourceHealthResult(
                status="disabled",
                reason="connector is disabled",
                auth_state=str(connector.auth_state or "not_required"),
                capabilities=self.capabilities(connector.kind),
                health={"checked": False},
            )

        kind = str(connector.kind).lower()
        value = str(connector.value or "").strip()
        capabilities = self.capabilities(kind)
        if not value:
            return SourceHealthResult(
                status="broken",
                reason="source value is empty",
                auth_state="not_required",
                capabilities=capabilities,
                health={"checked": True},
            )

        if kind in {"rss", "url", "web"}:
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return SourceHealthResult(
                    status="broken",
                    reason="source must be an absolute HTTP(S) URL",
                    auth_state="not_required",
                    capabilities=capabilities,
                    health={"checked": True},
                )
            started = time.monotonic()
            try:
                result = await self.web_probe(value)
            except Exception as exc:
                return SourceHealthResult(
                    status="broken",
                    reason=f"fetch failed: {type(exc).__name__}",
                    auth_state="not_required",
                    capabilities=capabilities,
                    health={
                        "checked": True,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    },
                )
            size = len(result) if isinstance(result, (str, bytes, bytearray)) else None
            return SourceHealthResult(
                status="healthy",
                reason=None,
                auth_state="not_required",
                capabilities=capabilities,
                health={
                    "checked": True,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "sample_size": size,
                },
                success=True,
            )

        if kind == "telegram":
            if self.telegram_probe is None:
                return SourceHealthResult(
                    status="unknown",
                    reason="MTProto connectivity probe is not attached",
                    auth_state="session_required",
                    capabilities=capabilities,
                    health={"checked": False},
                )
            started = time.monotonic()
            try:
                await self.telegram_probe(value)
            except Exception as exc:
                return SourceHealthResult(
                    status="auth_required" if "auth" in str(exc).lower() else "broken",
                    reason=f"telegram probe failed: {type(exc).__name__}",
                    auth_state="required" if "auth" in str(exc).lower() else "session_required",
                    capabilities=capabilities,
                    health={
                        "checked": True,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    },
                )
            return SourceHealthResult(
                status="healthy",
                reason=None,
                auth_state="ready",
                capabilities=capabilities,
                health={
                    "checked": True,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                },
                success=True,
            )

        if kind == "telegram_channel_dms":
            return SourceHealthResult(
                status="unknown",
                reason="Channel Direct Messages are ingested through the Bot API runtime",
                auth_state="not_required",
                capabilities=capabilities,
                health={"checked": False},
            )

        if kind == "local_browser":
            return SourceHealthResult(
                status="auth_required",
                reason="Local Collector connection is not configured",
                auth_state="local_required",
                capabilities=capabilities,
                health={"checked": False},
            )

        return SourceHealthResult(
            status="broken",
            reason=f"unsupported connector kind: {kind}",
            auth_state="unknown",
            capabilities=capabilities,
            health={"checked": True},
        )
