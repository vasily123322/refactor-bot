[![Type Check](https://img.shields.io/badge/Type%20check-mypy-blueviolet)](#)
[![Coverage](https://img.shields.io/badge/Coverage-pytest--cov-green)](#)

# refactor-bot

Telegram bot for channel operations: posting and scheduling, source ingestion, AI-assisted generation and editing, and the Studio frontend.

## How to Run

Activate the virtual environment first:

```bash
source .venv/bin/activate
```

From the project root (e.g., `/home/refactor_bot`), run the dispatcher entry point:

```bash
python -m app.bot.dispatcher
```

Alternatively, you can run the file directly:

```bash
python app/bot/dispatcher.py
```

For the supported production deployment, supervision, backup/restore, rollback, and
failed-start recovery contract, see `docs/deployment-recovery.md`.

## Database migrations

Alembic is the only supported schema authority for normal application startup and
future schema changes. It uses the same `DB_URL` and ORM registry as the application.

### Fresh/default database

A truly empty database is migrated to the shipped Alembic `head` automatically
during application startup, before the dispatcher or any background worker starts.
Startup then verifies the Alembic head, required ORM tables/columns, and (for SQLite)
foreign-key integrity/enforcement. There is no `Base.metadata.create_all()` or ad-hoc
schema fallback in the supported runtime path.

You can also initialize a fresh database explicitly before starting the application:

```bash
alembic upgrade head
```

### Existing Alembic-managed database

Application startup does **not** silently upgrade a managed database that is behind
the revision head shipped with the application. Deployments must run:

```bash
alembic upgrade head
```

before starting the new application version. If the database is behind, structurally
incomplete, or fails SQLite foreign-key integrity checks, startup fails closed before
workers start. Schema-changing migration failures are not swallowed or replaced by a
runtime bootstrap.

The first revision (`20260809_0001`) remains the frozen, non-destructive Alembic
baseline; later schema changes remain explicit revisions. No new migration is needed
for the startup-authority change itself because it does not change database schema.

### Health and readiness

The embedded Studio API exposes two unauthenticated operational endpoints with
different contracts:

- `GET /healthz` is **liveness** only. A 200 response means the Studio HTTP process
  can answer requests; it does not assert database or worker readiness.
- `GET /readyz` is **readiness**. It returns 200 only after the supported bot runtime
  has completed startup and the database is reachable at the Alembic head expected by
  the running application. During startup, shutdown, database failure, or schema
  mismatch it returns 503.

Readiness responses expose only coarse check states (`runtime`, `database`,
`schema`) and never connection strings, exception messages, tokens, or migration
details. Use `/healthz` for liveness/restart detection and `/readyz` for traffic
admission/draining. A persistent 503 on `/readyz` should trigger investigation of
application boot logs and database/schema state rather than bypassing the readiness
gate. Studio task termination after startup remains fatal to the parent runtime and is
supervised by the process lifecycle.

### Legacy database without `alembic_version`

A non-empty database without `alembic_version` is never modified by application
startup. This intentionally retires the old indefinite `create_all()`/ad-hoc
production bootstrap.

For a database created by the previous current-ORM `create_all()` startup path:

1. Stop the application and take a verified database backup.
2. Run the guarded one-time adoption command:

   ```bash
   python scripts/adopt_legacy_database.py
   ```

3. Restart the application.

The adoption command compares the unmanaged database against the complete current ORM
metadata, including Alembic-visible table/column/index/constraint differences. It
stamps the database at the current Alembic `head` only when that comparison is clean.
If the schema differs, adoption fails without stamping it. Repair or migrate that
database explicitly from the verified backup; do **not** bypass the guard with a blind
`alembic stamp head`.

After adoption, all future changes use normal Alembic upgrades. The legacy SQLite
repair helper in `app/core/db.py` is retained only for explicit/manual recovery of
historical layouts and is not part of application startup.

Useful checks:

```bash
alembic current --check-heads
alembic history
```

## Python dependencies

Python 3.12 is the supported production/canonical CI version. Install runtime and
test dependencies through the committed lock:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
```

See `docs/python-dependencies.md` for the lock format and intentional upgrade
workflow.

## How to Test

Make sure the virtual environment is activated:

```bash
source .venv/bin/activate
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

Set these to control runtime, logging, networking, and extraction behavior:

- LOG_LEVEL: stdout logging level (default `INFO`)
- LOG_FILE_ENABLED: keep the repository-managed `logs/bot.log` sink (default `true`); set `false` when an external collector owns persistence
- LOG_FILE_RETENTION: Loguru retention expression for rotated compressed file logs (default `14 days`)

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
- HTTP_FETCH_BACKOFF_INITIAL: HTML fetch backoff start seconds (default 0.4)
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

The repository file sink rotates daily, compresses rotated files, and retains them for
`LOG_FILE_RETENTION` (14 days by default). Set `LOG_FILE_ENABLED=false` when
stdout/stderr is collected by an external logging platform so the repository does not
persist a second copy on disk.