from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
	model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra='ignore')

	bot_token: str = Field(alias="BOT_TOKEN")
	api_id: int = Field(alias="API_ID")
	api_hash: str = Field(alias="API_HASH")
	redis_dsn: str | None = Field(default=None, alias="REDIS_DSN")
	# Backward-compat optional legacy keys; ignored if REDIS_DSN provided
	redis_host: str | None = Field(default=None, alias="redis_host")
	redis_port: int | None = Field(default=None, alias="redis_port")
	redis_db: int | None = Field(default=None, alias="redis_db")
	db_url: str = Field(default="sqlite+aiosqlite:///./data/bot.db", alias="DB_URL")
	log_level: str = Field(default="INFO", alias="LOG_LEVEL")

	# Scheduler settings
	repeat_overflow_limit: int = Field(default=2, alias="REPEAT_OVERFLOW_LIMIT")

	# SQLAlchemy: форсировать NullPool (всегда без пула)
	sqla_nullpool: bool = Field(default=False, alias="SQLA_NULLPOOL")
	# SQLAlchemy: для SQLite — StaticPool (один процессный коннект)
	sqla_staticpool: bool = Field(default=False, alias="SQLA_STATICPOOL")

	# Admin
	admin_user_id: int | None = Field(default=None, alias="ADMIN_USER_ID")
	admin_username: str | None = Field(default=None, alias="ADMIN_USERNAME")

	# OpenRouter / LLM
	openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
	openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL")
	openrouter_model: str = Field(default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL")
	openrouter_temperature: float = Field(default=0.7, alias="OPENROUTER_TEMPERATURE")
	openrouter_top_p: float = Field(default=0.7, alias="OPENROUTER_TOP_P")

	# Unified models config (menu)
	ai_models_json: str | None = Field(default=None, alias="AI_MODELS_JSON")

	# Direct providers (optional)
	openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
	anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
	gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
	deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")

	# Provider base URLs (defaults for public APIs)
	anthropic_base_url: str = Field(default="https://api.anthropic.com", alias="ANTHROPIC_BASE_URL")
	gemini_base_url: str = Field(default="https://generativelanguage.googleapis.com", alias="GEMINI_BASE_URL")
	deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")

	# OpenAI (для транскрипции Whisper)
	whisper_model: str = Field(default="whisper-1", alias="WHISPER_MODEL")

	# Groq (бесплатная транскрибация через OpenAI-совместимый API)
	speech_provider: str = Field(default="openai", alias="SPEECH_PROVIDER")  # openai|groq
	groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
	groq_base_url: str = Field(default="https://api.groq.com/openai/v1", alias="GROQ_BASE_URL")
	groq_whisper_model: str = Field(default="whisper-large-v3", alias="GROQ_WHISPER_MODEL")

	def effective_redis_dsn(self) -> str | None:
		if self.redis_dsn:
			return self.redis_dsn
		if self.redis_host and self.redis_port is not None and self.redis_db is not None:
			return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"
		return None

settings = Settings()  # Singleton 