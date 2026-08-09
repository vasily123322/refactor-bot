[![Type Check](https://img.shields.io/badge/Type%20check-mypy-blueviolet)](#)
[![Coverage](https://img.shields.io/badge/Coverage-pytest--cov-green)](#)

# Project Title

This is a brief description of the project.

## How to Run

Activate the virtual environment first:

```bash
source venv/bin/activate
```

From the project root (e.g., `/home/refactor_bot`), run the dispatcher entry point:

```bash
python -m app.bot.dispatcher
```

Alternatively, you can run the file directly:

```bash
python app/bot/dispatcher.py
```

## Database migrations

Alembic is the schema-evolution boundary for new database changes. It uses the same
`DB_URL` and ORM registry as the application.

Before deploying code with a new schema revision, run:

```bash
alembic upgrade head
```

The first revision (`20260809_0001`) is a non-destructive adoption baseline. On an
empty database it creates the current ORM schema. On an existing current database it
creates only missing tables and records the Alembic version; it does not drop tables
or rewrite existing data. The historical SQLite compatibility shim in
`init_db_if_needed_sync()` remains temporarily for older installations, but future
schema changes should be implemented as explicit Alembic revisions rather than new
ad-hoc `ALTER TABLE` code.

Useful checks:

```bash
alembic current
alembic history
```

## How to Test

Make sure the virtual environment is activated:

```bash
source venv/bin/activate
```

Then run all tests with coverage:

```bash
pytest -q --maxfail=1 --disable-warnings --cov=app --cov-report=term-missing
```

Run type checks:

```bash
mypy app
```

## Environment variables

Set these to control networking and extraction behavior:

- OPENROUTER_API_KEY: API key for OpenRouter
- OPENROUTER_BASE_URL: Default `https://openrouter.ai/api/v1`
- OPENROUTER_MODEL: Single model code in use (e.g. `openai/gpt-4o-mini`)
- OPENROUTER_TEMPERATURE: Default 0.7
- OPENROUTER_TOP_P: Default 0.7
- OPENROUTER_TIMEOUT_SECONDS: LLM HTTP timeout (default 60)
- OPENROUTER_MAX_RETRIES: LLM retries (default 3)
- OPENROUTER_BACKOFF_INITIAL: LLM backoff start seconds (default 0.5)
- OPENROUTER_BACKOFF_MAX: LLM backoff cap seconds (default 5.0)
- HTTP_FETCH_TIMEOUT_SECONDS: HTML fetch timeout (default 30)
- HTTP_FETCH_MAX_RETRIES: HTML fetch retries (default 2)
- HTTP_FETCH_BACKOFF_INITIAL: HTML backoff start seconds (default 0.4)
- HTTP_FETCH_BACKOFF_MAX: HTML backoff cap seconds (default 3.0)
- HTTP_FETCH_USER_AGENT: User-Agent header for fetcher (browser-like default)
- CONTENT_EXTRACT_MAX_LEN: Max extracted text length before LLM (default 8000)

## Modules overview (new/updated)

- app/services/llm/openrouter_client.py: OpenRouter client with retries, backoff, request_id, typed result
- app/services/llm/prompt_builder.py: PromptBuilder centralizes prompt construction
- app/services/notifier.py: Token quota alerts (80%)
- app/services/http/fetcher.py: fetch_html with retries, backoff and User-Agent
- app/services/extractors/html.py: HTML → text extractor with lxml/html.parser fallback
- app/services/ai_generation.py: unified LLM pipeline, instruction mapping helper, request_id propagation

## Observability

Each LLM call is logged with: request_id, model, duration (ms), tokens and outcome.
