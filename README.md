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
empty database it creates the frozen baseline schema. On an existing current database
it creates only missing baseline tables and records the Alembic version; it does not
drop tables or rewrite existing data. Later schema changes are explicit revisions
(e.g. `20260809_0002` for scheduler execution leases).

Once a database contains `alembic_version`, application startup treats it as
**Alembic-managed**. Managed databases do not run runtime `Base.metadata.create_all()`
or the historical SQLite ad-hoc schema shim. Startup fails before workers are started
when the database is behind the revision head shipped with the application. Apply:

```bash
alembic upgrade head
```

and start the application again.

Databases that have not adopted Alembic yet retain the historical compatibility
bootstrap so existing installations continue to start. This unmanaged path is
transitional; future schema changes should be implemented only as Alembic revisions.

Useful checks:

```bash
alembic current --check-heads
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
