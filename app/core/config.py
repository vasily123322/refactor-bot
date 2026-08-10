from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from loguru import logger
import json
from typing import Any


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = Field(alias="BOT_TOKEN")
    api_id: int = Field(alias="API_ID")
    api_hash: str = Field(alias="API_HASH")
    userbot_session: str | None = Field(default=None, alias="USERBOT_SESSION")
    userbot_proxy_url: str | None = Field(default=None, alias="USERBOT_PROXY_URL")
    userbot_proxy_scheme: str | None = Field(default=None, alias="USERBOT_PROXY_SCHEME")
    userbot_proxy_host: str | None = Field(default=None, alias="USERBOT_PROXY_HOST")
    userbot_proxy_port: int | None = Field(default=None, alias="USERBOT_PROXY_PORT")
    userbot_proxy_username: str | None = Field(default=None, alias="USERBOT_PROXY_USERNAME")
    userbot_proxy_password: str | None = Field(default=None, alias="USERBOT_PROXY_PASSWORD")
    redis_dsn: str | None = Field(default=None, alias="REDIS_DSN")
    # Backward-compat optional legacy keys; ignored if REDIS_DSN provided
    redis_host: str | None = Field(default=None, alias="redis_host")
    redis_port: int | None = Field(default=None, alias="redis_port")
    redis_db: int | None = Field(default=None, alias="redis_db")
    db_url: str = Field(default="sqlite+aiosqlite:///./data/bot.db", alias="DB_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Scheduler settings
    repeat_overflow_limit: int = Field(default=2, alias="REPEAT_OVERFLOW_LIMIT")

    # Opt-in cleanup for canonicalized unsuccessful legacy scheduler rows.
    post_task_retention_enabled: bool = Field(
        default=False, alias="POST_TASK_RETENTION_ENABLED"
    )
    post_task_retention_days: int = Field(
        default=90, alias="POST_TASK_RETENTION_DAYS"
    )
    post_task_retention_batch_size: int = Field(
        default=100, alias="POST_TASK_RETENTION_BATCH_SIZE"
    )
    post_task_retention_interval_seconds: int = Field(
        default=3600, alias="POST_TASK_RETENTION_INTERVAL_SECONDS"
    )

    # Opt-in canonical-only time-based autodelete runtime. Disabled by default until
    # operators explicitly choose the new lease-backed destructive worker.
    publication_autodelete_worker_enabled: bool = Field(
        default=False, alias="PUBLICATION_AUTODELETE_WORKER_ENABLED"
    )
    publication_autodelete_worker_interval_seconds: int = Field(
        default=60, alias="PUBLICATION_AUTODELETE_WORKER_INTERVAL_SECONDS"
    )
    publication_autodelete_worker_batch_size: int = Field(
        default=25, alias="PUBLICATION_AUTODELETE_WORKER_BATCH_SIZE"
    )
    publication_autodelete_worker_lease_ttl_seconds: int = Field(
        default=180, alias="PUBLICATION_AUTODELETE_WORKER_LEASE_TTL_SECONDS"
    )

    # Optional deterministic Inbox enrichment worker (never uses AI providers)
    local_enrichment_worker_enabled: bool = Field(
        default=False, alias="LOCAL_ENRICHMENT_WORKER_ENABLED"
    )
    local_enrichment_worker_interval_seconds: float = Field(
        default=15.0, alias="LOCAL_ENRICHMENT_WORKER_INTERVAL_SECONDS"
    )
    local_enrichment_worker_batch_size: int = Field(
        default=20, alias="LOCAL_ENRICHMENT_WORKER_BATCH_SIZE"
    )
    local_enrichment_worker_candidate_timeout_seconds: float = Field(
        default=10.0, alias="LOCAL_ENRICHMENT_WORKER_CANDIDATE_TIMEOUT_SECONDS"
    )

    # SQLAlchemy: форсировать NullPool (всегда без пула)
    sqla_nullpool: bool = Field(default=False, alias="SQLA_NULLPOOL")
    # SQLAlchemy: для SQLite — StaticPool (один процессный коннект)
    sqla_staticpool: bool = Field(default=False, alias="SQLA_STATICPOOL")

    # Admin
    admin_user_id: int | None = Field(default=None, alias="ADMIN_USER_ID")
    admin_username: str | None = Field(default=None, alias="ADMIN_USERNAME")

    # OpenRouter / LLM
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    openrouter_model: str = Field(
        default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL"
    )
    openrouter_temperature: float = Field(default=0.7, alias="OPENROUTER_TEMPERATURE")
    openrouter_top_p: float = Field(default=0.7, alias="OPENROUTER_TOP_P")

    # OpenRouter network policy
    openrouter_timeout_seconds: float = Field(
        default=60.0, alias="OPENROUTER_TIMEOUT_SECONDS"
    )
    openrouter_max_retries: int = Field(default=3, alias="OPENROUTER_MAX_RETRIES")
    openrouter_backoff_initial: float = Field(
        default=0.5, alias="OPENROUTER_BACKOFF_INITIAL"
    )
    openrouter_backoff_max: float = Field(default=5.0, alias="OPENROUTER_BACKOFF_MAX")

    # Unified models config (menu)
    ai_models_json: str | None = Field(default=None, alias="AI_MODELS_JSON")

    # Direct providers (optional)
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")

    # Provider base URLs (defaults for public APIs)
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com", alias="ANTHROPIC_BASE_URL"
    )
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com", alias="GEMINI_BASE_URL"
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )

    # OpenAI (для транскрипции Whisper)
    whisper_model: str = Field(default="whisper-1", alias="WHISPER_MODEL")

    # Groq (бесплатная транскрибация через OpenAI-совместимый API)
    speech_provider: str = Field(
        default="openai", alias="SPEECH_PROVIDER"
    )  # openai|groq
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    groq_base_url: str = Field(
        default="https://api.groq.com/openai/v1", alias="GROQ_BASE_URL"
    )
    groq_whisper_model: str = Field(
        default="whisper-large-v3", alias="GROQ_WHISPER_MODEL"
    )

    # Generic HTTP fetch (HTML)
    http_fetch_timeout_seconds: float = Field(
        default=30.0, alias="HTTP_FETCH_TIMEOUT_SECONDS"
    )
    http_fetch_max_retries: int = Field(default=2, alias="HTTP_FETCH_MAX_RETRIES")
    http_fetch_backoff_initial: float = Field(
        default=0.4, alias="HTTP_FETCH_BACKOFF_INITIAL"
    )
    http_fetch_backoff_max: float = Field(default=3.0, alias="HTTP_FETCH_BACKOFF_MAX")

    # HTTP fetch headers
    http_fetch_user_agent: str = Field(
        default="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
        alias="HTTP_FETCH_USER_AGENT",
    )

    # Content extraction
    content_extract_max_len: int = Field(default=8000, alias="CONTENT_EXTRACT_MAX_LEN")

    @field_validator("userbot_proxy_port", mode="before")
    @classmethod
    def _empty_proxy_port_to_none(cls, value):
        if value == "":
            return None
        return value

    def effective_redis_dsn(self) -> str | None:
        if self.redis_dsn:
            return self.redis_dsn
        if (
            self.redis_host
            and self.redis_port is not None
            and self.redis_db is not None
        ):
            return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return None

    def get_ai_models_config(self) -> dict[str, Any] | None:
        """Разобрать и валидировать AI_MODELS_JSON.

        Возвращает dict или None, если переменная не задана или некорректна.
        Пишет предупреждения в лог при ошибках.
        """
        if not self.ai_models_json:
            return None
        try:
            parsed = json.loads(self.ai_models_json)
        except Exception as e:
            logger.warning(f"AI_MODELS_JSON: некорректный JSON: {e}")
            return None
        if not isinstance(parsed, dict):
            logger.warning("AI_MODELS_JSON: ожидался объект JSON (mapping)")
            return None
        # Лёгкая проверка значений: строки или объекты
        for k, v in list(parsed.items()):
            if not isinstance(k, str):
                logger.warning(
                    "AI_MODELS_JSON: ключи должны быть строками — запись отброшена"
                )
                parsed.pop(k, None)
                continue
            if not (isinstance(v, str) or isinstance(v, dict)):
                logger.warning(
                    f"AI_MODELS_JSON: значение для '{k}' должно быть строкой или объектом — отброшено"
                )
                parsed.pop(k, None)
        return parsed or None


settings = Settings()  # Singleton